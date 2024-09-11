import os
from contextlib import nullcontext
import datetime
from typing import Literal

import torch
import torch.distributed as dist
from pydantic_config import parse_argv, BaseConfig
from torch.distributed import destroy_process_group, init_process_group

from torchdata.stateful_dataloader import StatefulDataLoader
from transformers import (
    AutoTokenizer,
    DataCollatorForLanguageModeling,
    LlamaConfig,
    LlamaForCausalLM,
    get_cosine_schedule_with_warmup,
)
from torch.distributed.fsdp import (
    FullyShardedDataParallel as FSDP,
    MixedPrecision,
)
from torch.distributed.device_mesh import init_device_mesh
from hivemind.optim.optimizer import logger
from open_diloco.utils import (
    FakeTokenizedDataset,
    get_sharding_strategy,
    WandbLogger,
    DummyLogger,
)
from datasets import load_dataset
from datasets.distributed import split_dataset_by_node

TIMEOUT_NCCL_MINUTES = os.environ.get("TIMEOUT_NCCL_MINUTES", 120)
TEST_VOCAB_SIZE = 1024


# Function to initialize the distributed process group
def ddp_setup():
    init_process_group(backend="nccl", timeout=datetime.timedelta(minutes=TIMEOUT_NCCL_MINUTES))
    torch.cuda.set_device(int(os.environ["LOCAL_RANK"]))


def log(message):
    logger.info(f"[rank {os.environ['LOCAL_RANK']}] {message}")


class DilocoConfig(BaseConfig):
    outer_lr: float = 0.7
    local_steps: int = 10


class Config(BaseConfig):
    diloco: DilocoConfig = DilocoConfig()
    path_model: str = "PrimeIntellect/llama-150m-fresh"
    torch_compile: bool = True
    attn_implementation: str = "flash_attention_2"
    # Data
    seq_length: int = 1024
    num_workers: int = 4
    # Optimization
    lr: float = 4e-4
    total_batch_size: int = 512
    per_device_train_batch_size: int = 32
    warmup_steps: int = 1000
    total_steps: int = 88_000
    sharding_strategy: str = "FULL_SHARD"
    project: str = "debug"
    metric_logger_type: Literal["wandb", "dummy"] = "wandb"
    fake_data: bool = False
    dataset_name_or_path: str = "allenai/c4"


def get_dataloader(tokenizer, world_size, rank, config: Config) -> StatefulDataLoader:
    if config.fake_data:
        train_dataset = FakeTokenizedDataset(config.seq_length, TEST_VOCAB_SIZE)
    else:
        ds = load_dataset(config.dataset_name_or_path, "en", streaming=True)

        def tokenize_function(data):
            outputs = tokenizer(
                data["text"],
                truncation=True,
                max_length=config.seq_length,
                padding="max_length",
            )
            return outputs

        tokenized_datasets = ds.map(tokenize_function, batched=True, remove_columns=["text", "timestamp", "url"])[
            "train"
        ]

        train_dataset = split_dataset_by_node(tokenized_datasets, world_size=world_size, rank=rank)

    data_collator = DataCollatorForLanguageModeling(tokenizer=tokenizer, mlm=False)

    return StatefulDataLoader(
        train_dataset,
        collate_fn=data_collator,
        batch_size=config.per_device_train_batch_size,
        num_workers=config.num_workers,
    )


def get_model(config: Config) -> LlamaForCausalLM:
    # Load model
    config_model = LlamaConfig.from_pretrained(config.path_model, attn_implementation=config.attn_implementation)
    return LlamaForCausalLM.from_pretrained(pretrained_model_name_or_path=config.path_model, config=config_model)


def get_offloaded_param(model: LlamaForCausalLM) -> list[torch.Tensor]:
    return [param.data.detach().clone().to("cuda") for param in model.parameters()]


def train(config: Config):
    sharding_strategy = get_sharding_strategy(config.sharding_strategy)
    local_rank = int(os.environ["LOCAL_RANK"])
    world_size = int(os.environ["WORLD_SIZE"])
    rank = int(os.environ["RANK"])

    # batch_size is the total batch size for all GPUs
    assert config.total_batch_size % world_size == 0
    batch_size = config.total_batch_size // world_size

    assert batch_size % config.per_device_train_batch_size == 0
    gradient_accumulation_steps = batch_size // config.per_device_train_batch_size

    tokenizer = AutoTokenizer.from_pretrained("mistralai/Mistral-7B-v0.1", use_fast=True)
    tokenizer.pad_token = "</s>"  # Ensure pad token is set for models that need it

    train_dataloader = get_dataloader(tokenizer, world_size, rank, config)

    model = get_model(config)
    model = model.to(local_rank)

    local_world_size = int(os.environ["LOCAL_WORLD_SIZE"])
    nnodes = world_size // local_world_size
    device_mesh = init_device_mesh("cuda", (nnodes, local_world_size), mesh_dim_names=("global", "local"))

    global_pg = device_mesh.get_group("global")
    local_pg = device_mesh.get_group("local")
    log(f"global pg world : {global_pg.size()}, local pg: {local_pg.size()}")

    model = FSDP(
        model,
        sharding_strategy=sharding_strategy,
        mixed_precision=MixedPrecision(param_dtype=torch.bfloat16),
        use_orig_params=config.torch_compile,
        process_group=local_pg,
    )
    if config.torch_compile:
        model = torch.compile(model)

    # Setup optimizers
    inner_optimizer = torch.optim.AdamW(model.parameters(), lr=config.lr, weight_decay=0.1, betas=(0.9, 0.95))

    cpu_model = get_offloaded_param(
        model
    )  # todo: in case of sharded grap op we need to offload the cpu model only once per nodes
    outer_optimizer = torch.optim.SGD(cpu_model, lr=config.diloco.outer_lr, momentum=0.9, nesterov=True)

    scheduler = get_cosine_schedule_with_warmup(
        inner_optimizer,
        num_warmup_steps=config.warmup_steps,
        num_training_steps=config.total_steps,
    )

    model.train()

    if rank == 0:
        logger_cls = WandbLogger if config.metric_logger_type == "wandb" else DummyLogger
        metric_logger = logger_cls(project=config.project, config=config.model_dump(), resume=False)

    loss_batch = 0

    train_dataloader_iterator = iter(train_dataloader)

    outer_step = 0
    while True:
        if rank == 0:
            log(f"outer_step step: {outer_step}")
        for inner_step in range(config.diloco.local_steps):
            for grad_acc_step in range(gradient_accumulation_steps):
                is_accumulating = grad_acc_step < gradient_accumulation_steps - 1
                batch = next(train_dataloader_iterator)

                for key in batch.keys():
                    batch[key] = batch[key].to("cuda")

                with model.no_sync() if is_accumulating else nullcontext():
                    outputs = model(**batch)
                    loss = outputs.loss / gradient_accumulation_steps
                    loss.backward()
                    loss_batch += loss.detach()

            model.clip_grad_norm_(1.0)  # gradient clipping
            inner_optimizer.step()
            scheduler.step()
            inner_optimizer.zero_grad()

            if rank == 0:
                real_step = outer_step * config.diloco.local_steps + inner_step + 1
                inner_lr = [group["lr"] for group in inner_optimizer.param_groups][0]

                metrics = {
                    "Loss": loss_batch.item(),
                    "step": real_step,
                    "inner_lr": inner_lr,
                }

                metric_logger.log(metrics)

                log(f"step: {real_step}, loss: {loss_batch.item()}, inner_lr: {inner_lr}")

            loss_batch = 0

        ### the whole sectione below is just a PoC. We need to benchmark and optimizer what is the most efficient:
        ## do the all reduce on cpu or on gpu
        ## do the outer optimizer step on cpu or on gpu

        for param_offloaded, param in zip(
            cpu_model, model.parameters()
        ):  # There is only one big fat tensor in the param because of fsdp 1 bucket stuff
            # todo check how to handle the SHARD_GRAD_OP strategy where the weight are replicated across the local devices
            param_offloaded.grad = param_offloaded.data - param.data.to(param_offloaded.device)
            dist.all_reduce(param_offloaded.grad, op=dist.ReduceOp.AVG, group=global_pg)

        outer_optimizer.step()
        outer_optimizer.zero_grad()

        for param_offloaded, param in zip(cpu_model, model.parameters()):
            param.data = param_offloaded.data.to("cuda")

        outer_step += 1

    if rank == 0:
        metric_logger.finish()


if __name__ == "__main__":
    # Allow eager fallback during production so that that the training runs dont die
    # However, in development, we want to know that we broke torch compile
    torch._dynamo.config.suppress_errors = "PRIME_INTELLECT_DEV" not in os.environ
    torch.set_float32_matmul_precision("high")
    ddp_setup()
    config = Config(**parse_argv())
    train(config)
    destroy_process_group()