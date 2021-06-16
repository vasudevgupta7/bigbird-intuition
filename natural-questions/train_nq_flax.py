import json
import os
from dataclasses import dataclass
from functools import partial
from typing import Callable

import numpy as np
import wandb
import flax.linen as nn
import jax
import jax.numpy as jnp
import joblib
import optax
from datasets import load_dataset
from flax import traverse_util
from flax import jax_utils
from flax.serialization import from_bytes, to_bytes
from flax.training import train_state
from flax.training.common_utils import shard
from tqdm.auto import tqdm

from transformers import (
    BigBirdConfig,
    BigBirdTokenizerFast,
    FlaxBigBirdForQuestionAnswering
)
from transformers.models.big_bird.modeling_flax_big_bird import \
    FlaxBigBirdForQuestionAnsweringModule

##########################################################
# How can we inherit from HuggingFace Flax Transformers?
# explore it below 👇👇
##########################################################


class FlaxBigBirdForNaturalQuestionsModule(FlaxBigBirdForQuestionAnsweringModule):
    """
    BigBirdForQuestionAnswering with CLS Head over the top for predicting category

    This way we can load its weights with FlaxBigBirdForQuestionAnswering
    """

    config: BigBirdConfig
    dtype: jnp.dtype = jnp.float32
    add_pooling_layer: bool = True

    def setup(self):
        super().setup()
        self.cls = nn.Dense(5, dtype=self.dtype)

    def __call__(self, *args, **kwargs):
        outputs = super().__call__(*args, **kwargs)
        cls_out = self.cls(outputs[2])
        return outputs[:2] + (cls_out, )


class FlaxBigBirdForNaturalQuestions(FlaxBigBirdForQuestionAnswering):
    module_class = FlaxBigBirdForNaturalQuestionsModule


def calculate_loss_for_nq(
    start_logits, start_labels, end_logits, end_labels, pooled_logits, pooler_labels
):
    def cross_entropy(logits, labels, reduction=None):
        """
        Args:
            logits: bsz, seqlen, vocab_size
            labels: bsz, seqlen
        """
        vocab_size = logits.shape[-1]
        labels = (labels[..., None] == jnp.arange(vocab_size)[None]).astype("f4")
        logits = jax.nn.log_softmax(logits, axis=-1)
        loss = -jnp.sum(labels * logits, axis=-1)
        if reduction is not None:
            loss = reduction(loss)
        return loss

    cross_entropy = partial(cross_entropy, reduction=jnp.mean)
    start_loss = cross_entropy(start_logits, start_labels)
    end_loss = cross_entropy(end_logits, end_labels)
    pooled_loss = cross_entropy(pooled_logits, pooler_labels)
    return (start_loss + end_loss + pooled_loss) / 3


@dataclass
class Args:
    model_id: str = "google/bigbird-roberta-base"
    eval_steps: int = 512
    save_steps: int = 512
    logging_steps: int = 512

    batch_size_per_device: int = 1
    max_epochs: int = 3

    # tx_args
    lr: float = 1e-4
    init_lr: float = 0.0
    warmup_steps: int = 100
    weight_decay: float = 1e-3

    save_dir: str = "bigbird-roberta-natural-questions"
    base_dir: str = "training-expt"
    tr_data_path: str = "data/nq-training.jsonl"
    val_data_path: str = "data/nq-validation.jsonl"

    def __post_init__(self):
        os.makedirs(self.base_dir, exist_ok=True)
        self.save_dir = os.path.join(self.base_dir, self.save_dir)
        self.batch_size = self.batch_size_per_device * jax.device_count()

        print("#################### AVAILABLE DEVICES ####################")
        print(jax.devices())
        print("###########################################################")


@dataclass
class DataCollator:

    pad_id: int
    max_length: int = 4096  # no dynamic padding on TPUs

    def __call__(self, batch):
        batch = self.collate_fn(batch)
        batch = jax.tree_map(shard, batch)
        return batch

    def collate_fn(self, features):
        input_ids, attention_mask = self.fetch_inputs(features["input_ids"])
        batch = {
            "input_ids": jnp.array(input_ids, dtype=jnp.int32),
            "attention_mask": jnp.array(attention_mask, dtype=jnp.int32),
            "start_labels": jnp.array(features["start_token"], dtype=jnp.int32),
            "end_labels": jnp.array(features["end_token"], dtype=jnp.int32),
            "pooled_labels": jnp.array(features["category"], dtype=jnp.int32),
        }
        return batch

    def fetch_inputs(self, input_ids: list):
        inputs = [self._fetch_inputs(ids) for ids in input_ids]
        return zip(*inputs)

    def _fetch_inputs(self, input_ids: list):
        attention_mask = [1 for _ in range(len(input_ids))]
        while len(input_ids) < self.max_length:
            input_ids.append(self.pad_id)
            attention_mask.append(0)
        return input_ids, attention_mask


def get_batched_dataset(dataset, batch_size, seed=None):
    if seed is not None:
        dataset = dataset.shuffle(seed=seed)
    for i in range(len(dataset) // batch_size):
        batch = dataset[i * batch_size : (i + 1) * batch_size]
        yield dict(batch)


@partial(jax.pmap, axis_name="batch")
def train_step(state, drp_rng, **model_inputs):

    def loss_fn(params):
        start_labels = model_inputs.pop("start_labels")
        end_labels = model_inputs.pop("end_labels")
        pooled_labels = model_inputs.pop("pooled_labels")

        outputs = state.apply_fn(
            **model_inputs, params=params, dropout_rng=drp_rng, train=True
        )
        start_logits, end_logits, pooled_logits = outputs

        return calculate_loss_for_nq(
            start_logits,
            start_labels,
            end_logits,
            end_labels,
            pooled_logits,
            pooled_labels,
        )

    drp_rng, new_drp_rng = jax.random.split(drp_rng)
    grad_fn = jax.value_and_grad(loss_fn)
    loss, grads = grad_fn(state.params)
    metrics = jax.lax.pmean({"loss": loss}, axis_name="batch")
    grads = jax.lax.pmean(grads, "batch")
    state = state.apply_gradients(grads=grads)
    return state, metrics, new_drp_rng


@partial(jax.pmap, axis_name="batch")
def val_step(state, **model_inputs):
    start_labels = model_inputs.pop("start_labels")
    end_labels = model_inputs.pop("end_labels")
    pooled_labels = model_inputs.pop("pooled_labels")

    outputs = state.apply_fn(
        **model_inputs, params=state.params, train=False
    )
    start_logits, end_logits, pooled_logits = outputs

    loss = calculate_loss_for_nq(
        start_logits, start_labels, end_logits, end_labels, pooled_logits, pooled_labels
    )
    metrics = jax.lax.pmean({"loss": loss}, axis_name="batch")
    return metrics


@dataclass
class Trainer:
    args: Args
    data_collator: Callable
    model_save_fn: Callable
    train_step_fn: Callable
    val_step_fn: Callable
    logger: wandb
    tr_num_samples: int = None
    val_num_samples: int = None
    scheduler_fn: Callable = None

    def create_state(self, model, tx, ckpt_dir=None):
        params = model.params
        state = train_state.TrainState.create(
            apply_fn=model.__call__, params=params, tx=tx
        )
        if ckpt_dir is not None:
            params, opt_state, step, args, data_collator = restore_checkpoint(
                ckpt_dir, state
            )
            tx_args = {
                "lr": args.lr,
                "init_lr": args.init_lr,
                "warmup_steps": args.warmup_steps,
                "num_train_steps": self.tr_num_samples // args.batch_size,
                "weight_decay": args.weight_decay,
            }
            tx, lr = build_tx(**tx_args)
            state = train_state.TrainState(
                step=step,
                apply_fn=model.__call__,
                params=params,
                tx=tx,
                opt_state=opt_state,
            )
            self.args = args
            self.data_collator = data_collator
            self.scheduler_fn = lr
            model.params = params
        state = jax_utils.replicate(state)
        return state

    def train(self, state, tr_dataset, val_dataset):
        args = self.args
        val_dataloader = get_batched_dataset(val_dataset, args.batch_size)
        total = len(tr_dataset) // args.batch_size

        rng = jax.random.PRNGKey(0)
        drp_rng = jax.random.split(rng, jax.device_count())
        for epoch in range(args.max_epochs):
            running_loss = jnp.array(0, dtype=jnp.float32)
            tr_dataloader = get_batched_dataset(tr_dataset, args.batch_size, seed=epoch)
            for batch in tqdm(tr_dataloader, total=total, desc=f"Running EPOCH-{epoch}"):
                batch = self.data_collator(batch)
                state, metrics, drp_rng = self.train_step_fn(state, drp_rng, **batch)
                running_loss += jax_utils.unreplicate(metrics["loss"])
                state_step = jax_utils.unreplicate(state.step)

                eval_loss = None
                if state_step % args.eval_steps == 0:
                    eval_loss = self.evaluate(state, val_dataloader)

                if state_step % args.logging_steps == 0:
                    tr_loss = running_loss / (state_step + 1)
                    tqdm.write("############### LOGGING ###############")
                    lr = self.scheduler_fn(state_step - 1)
                    logging_dict = dict(tr_loss=tr_loss.item(), lr=lr.item(), step=state_step.item())
                    if eval_loss is not None:
                        logging_dict["eval_loss"] = eval_loss.item()
                    tqdm.write(str(logging_dict))
                    self.logger.log(logging_dict)
                    tqdm.write("#######################################")

                if state_step % args.save_steps == 0:
                    self.save_checkpoint(args.save_dir + f"-epoch-{epoch}", state=state)

        return tr_loss, eval_loss

    def evaluate(self, state, dataloader):
        total = self.val_num_samples // self.args.batch_size
        running_loss = jnp.array(0, dtype=jnp.float32)
        i = 0
        for batch in tqdm(dataloader, total=total, desc="Evaluating ... "):
            batch = self.data_collator(batch)
            metrics = self.val_step_fn(state, **batch)
            running_loss += metrics["loss"][0]
            i += 1
        return running_loss / (i + 1)

    def save_checkpoint(self, save_dir, state):
        state = jax_utils.unreplicate(state)
        print(f"SAVING CHECKPOINT IN {save_dir}", end=" ... ")
        self.model_save_fn(save_dir, params=state.params)
        with open(os.path.join(save_dir, "opt_state.msgpack"), "wb") as f:
            f.write(to_bytes(state.opt_state))
        joblib.dump(args, os.path.join(save_dir, "args.joblib"))
        joblib.dump(self.data_collator, os.path.join(save_dir, "data_collator.joblib"))
        with open(os.path.join(save_dir, "training_state.json"), "w") as f:
            json.dump({"step": state.step.item()}, f)
        print("DONE")


def restore_checkpoint(save_dir, state):
    print(f"RESTORING CHECKPOINT FROM {save_dir}", end=" ... ")
    with open(os.path.join(save_dir, "flax_model.msgpack"), "rb") as f:
        params = from_bytes(state.params, f.read())

    with open(os.path.join(save_dir, "opt_state.msgpack"), "rb") as f:
        opt_state = from_bytes(state.opt_state, f.read())

    args = joblib.load(os.path.join(save_dir, "args.joblib"))
    data_collator = joblib.load(os.path.join(save_dir, "data_collator.joblib"))

    with open(os.path.join(save_dir, "training_state.json"), "r") as f:
        training_state = json.load(f)
    step = training_state["step"]

    print("DONE")
    return params, opt_state, step, args, data_collator


def scheduler_fn(lr, init_lr, warmup_steps, num_train_steps):
    decay_steps = num_train_steps - warmup_steps
    warmup_fn = optax.linear_schedule(
        init_value=init_lr, end_value=lr, transition_steps=warmup_steps
    )
    decay_fn = optax.linear_schedule(
        init_value=lr, end_value=1e-7, transition_steps=decay_steps
    )
    lr = optax.join_schedules(
        schedules=[warmup_fn, decay_fn], boundaries=[warmup_steps]
    )
    return lr


def build_tx(lr, init_lr, warmup_steps, num_train_steps, weight_decay):
    def weight_decay_mask(params):
        params = traverse_util.flatten_dict(params)
        mask = {
            k: (v[-1] != "bias" and v[-2:] != ("LayerNorm", "scale"))
            for k, v in params.items()
        }
        return traverse_util.unflatten_dict(mask)

    lr = scheduler_fn(lr, init_lr, warmup_steps, num_train_steps)

    tx = optax.adamw(
        learning_rate=lr, weight_decay=weight_decay, mask=weight_decay_mask
    )
    return tx, lr


if __name__ == "__main__":
    args = Args()
    print(args)

    tr_dataset = load_dataset("json", data_files=args.tr_data_path)["train"]
    val_dataset = load_dataset("json", data_files=args.val_data_path)["train"]

    if os.environ.get("TRAIN_ON_SMALL", "FALSE") == "TRUE":
        np.random.seed(0)
        indices = np.random.randint(0, 298152, size=8000)
        tr_dataset = tr_dataset.select(indices)
        np.random.seed(0)
        indices = np.random.randint(0, 9000, size=1000)
        val_dataset = val_dataset.select(indices)

    print(tr_dataset)
    print(val_dataset)

    model = FlaxBigBirdForNaturalQuestions.from_pretrained(args.model_id)
    tokenizer = BigBirdTokenizerFast.from_pretrained(args.model_id)
    data_collator = DataCollator(pad_id=tokenizer.pad_token_id, max_length=4096)

    tx_args = {
        "lr": args.lr,
        "init_lr": args.init_lr,
        "warmup_steps": args.warmup_steps,
        "num_train_steps": args.max_epochs * (len(tr_dataset) // args.batch_size),
        "weight_decay": args.weight_decay,
    }
    tx, lr = build_tx(**tx_args)

    logger = wandb.init(project="bigbird-natural-questions", config=args.__dict__)
    trainer = Trainer(
        args=args,
        data_collator=data_collator,
        model_save_fn=model.save_pretrained,
        train_step_fn=train_step,
        val_step_fn=val_step,
        logger=logger,
        tr_num_samples=len(tr_dataset),
        val_num_samples=len(val_dataset),
        scheduler_fn=lr,
    )

    ckpt_dir = None # "training-expt/bigbird-roberta-natural-questions-epoch-0"
    state = trainer.create_state(model, tx, ckpt_dir=ckpt_dir)
    try:
        trainer.train(state, tr_dataset, val_dataset)
    except KeyboardInterrupt:
        print("Oooops; TRAINING STOPPED UNFORTUNATELY")

    print("SAVING WEIGHTS IN `final-weights`")
    params = jax_utils.unreplicate(state.params)
    model.save_pretrained(os.path.join(args.base_dir, "final-weights"), params=params)
