from functools import partial
from pprint import pformat
from typing import Any

import ignite.distributed as idist
import yaml
from data import denormalize, download_datasets, setup_data
from ignite.engine import Events
from ignite.metrics import ConfusionMatrix, IoU, mIoU
from ignite.utils import manual_seed
from torch import nn, optim
from torch.optim.lr_scheduler import LambdaLR
from torch.utils.data.distributed import DistributedSampler
from torchvision.models.segmentation import deeplabv3_resnet101
from trainers import setup_evaluator, setup_trainer
from utils import *
from vis import predictions_gt_images_handler


def run(local_rank: int, config: Any):

    # make a certain seed
    rank = idist.get_rank()
    manual_seed(config.seed + rank)

    # create output folder
    config.output_dir = setup_output_dir(config, rank)

    # donwload datasets and create dataloaders
    dataloader_train, dataloader_eval = setup_data(config)
    le = len(dataloader_train)

    # model, optimizer, loss function, device
    device = idist.device()
    model = idist.auto_model(
        deeplabv3_resnet101(num_classes=config.num_classes)
    )
    optimizer = idist.auto_optim(
        optim.SGD(
            model.parameters(),
            lr=1.0,
            momentum=0.9,
            weight_decay=5e-4,
            nesterov=False,
        )
    )
    loss_fn = nn.CrossEntropyLoss().to(device=device)
    lr_scheduler = LambdaLR(
        optimizer,
        lr_lambda=[
            partial(
                lambda_lr_scheduler,
                lr0=config.lr,
                n=config.max_epochs * le,
                a=0.9,
            )
        ],
    )

    # setup metrics
    cm_metric = ConfusionMatrix(num_classes=config.num_classes)
    metrics = {"IoU": IoU(cm_metric), "mIoU_bg": mIoU(cm_metric)}

    # trainer and evaluator
    trainer = setup_trainer(config, model, optimizer, loss_fn, device)
    evaluator = setup_evaluator(config, model, metrics, device)

    # setup engines logger with python logging
    # print training configurations
    logger = setup_logging(config)
    logger.info("Configuration: \n%s", pformat(vars(config)))
    (config.output_dir / "config-lock.yaml").write_text(yaml.dump(config))
    trainer.logger = evaluator.logger = logger

    # set epoch for distributed sampler
    @trainer.on(Events.EPOCH_STARTED)
    def set_epoch():
        if idist.get_world_size() > 1 and isinstance(
            dataloader_train.sampler, DistributedSampler
        ):
            dataloader_train.sampler.set_epoch(trainer.state.epoch - 1)

    # setup ignite handlers
    #::: if (it.save_training || it.save_evaluation || it.patience || it.terminate_on_nan || it.timer || it.limit_sec) { :::#

    #::: if (it.save_training) { :::#
    to_save_train = {
        "model": model,
        "optimizer": optimizer,
        "trainer": trainer,
        "lr_scheduler": lr_scheduler,
    }
    #::: } else { :::#
    to_save_train = None
    #::: } :::#

    #::: if (it.save_evaluation) { :::#
    to_save_eval = {"model": model}
    #::: } else { :::#
    to_save_eval = None
    #::: } :::#

    ckpt_handler_train, ckpt_handler_eval, timer = setup_handlers(
        trainer, evaluator, config, to_save_train, to_save_eval
    )
    #::: } :::#

    # experiment tracking
    #::: if (it.logger) { :::#
    if rank == 0:
        exp_logger = setup_exp_logging(config, trainer, optimizer, evaluator)

        # Log validation predictions as images
        # We define a custom event filter to log less frequently the images (to reduce storage size)
        # - we plot images with masks of the middle validation batch
        # - once every 3 validations and
        # - at the end of the training
        def custom_event_filter(_, val_iteration):
            c1 = val_iteration == len(dataloader_eval) // 2
            c2 = trainer.state.epoch % 3 == 0
            c2 |= trainer.state.epoch == config.max_epochs
            return c1 and c2

        # Image denormalization function to plot predictions with images
        mean = (0.485, 0.456, 0.406)
        std = (0.229, 0.224, 0.225)
        img_denormalize = partial(denormalize, mean=mean, std=std)

        exp_logger.attach(
            evaluator,
            log_handler=predictions_gt_images_handler(
                img_denormalize_fn=img_denormalize,
                n_images=15,
                another_engine=trainer,
                prefix_tag="validation",
            ),
            event_name=Events.ITERATION_COMPLETED(
                event_filter=custom_event_filter
            ),
        )

    #::: } :::#

    # print metrics to the stderr
    # with `add_event_handler` API
    # for training stats
    trainer.add_event_handler(
        Events.ITERATION_COMPLETED(every=config.log_every_iters),
        log_metrics,
        tag="train",
    )

    # run evaluation at every training epoch end
    # with shortcut `on` decorator API and
    # print metrics to the stderr
    # again with `add_event_handler` API
    # for evaluation stats
    @trainer.on(Events.EPOCH_COMPLETED(every=1))
    def _():
        # show timer
        #::: if (it.save_training || it.save_evaluation || it.patience || it.terminate_on_nan || it.timer || it.limit_sec) { :::#
        if timer is not None:
            logger.info("Time per batch: %.4f seconds", timer.value())
            timer.reset()
        #::: } :::#

        evaluator.run(dataloader_eval, epoch_length=config.eval_epoch_length)
        log_metrics(evaluator, "eval")

    # let's try run evaluation first as a sanity check
    @trainer.on(Events.STARTED)
    def _():
        evaluator.run(dataloader_eval, epoch_length=config.eval_epoch_length)

    # setup if done. let's run the training
    trainer.run(
        dataloader_train,
        max_epochs=config.max_epochs,
        epoch_length=config.train_epoch_length,
    )

    # close logger
    #::: if (it.logger) { :::#
    if rank == 0:
        from ignite.contrib.handlers.wandb_logger import WandBLogger

        if isinstance(exp_logger, WandBLogger):
            # why handle differently for wandb?
            # See: https://github.com/pytorch/ignite/issues/1894
            exp_logger.finish()
        elif exp_logger:
            exp_logger.close()
    #::: } :::#

    # show the last checkpoint filename
    #::: if (it.save_training || it.save_evaluation || it.patience || it.terminate_on_nan || it.timer || it.limit_sec) { :::#
    if ckpt_handler_train is not None:
        logger.info(
            "Last training checkpoint name - %s",
            ckpt_handler_train.last_checkpoint,
        )

    if ckpt_handler_eval is not None:
        logger.info(
            "Last evaluation checkpoint name - %s",
            ckpt_handler_eval.last_checkpoint,
        )
    #::: } :::#


# main entrypoint
def main():
    config = setup_parser().parse_args()
    download_datasets(config.data_path)
    #::: if (it.dist === 'spawn') { :::#
    #::: if (it.nproc_per_node && it.nnodes > 1 && it.master_addr && it.master_port) { :::#
    kwargs = {
        "nproc_per_node": config.nproc_per_node,
        "nnodes": config.nnodes,
        "node_rank": config.node_rank,
        "master_addr": config.master_addr,
        "master_port": config.master_port,
    }
    #::: } else if (it.nproc_per_node) { :::#
    kwargs = {"nproc_per_node": config.nproc_per_node}
    #::: } :::#
    with idist.Parallel(config.backend, **kwargs) as p:
        p.run(run, config=config)
    #::: } else { :::#
    with idist.Parallel(config.backend) as p:
        p.run(run, config=config)
    #::: } :::#


if __name__ == "__main__":
    main()