import inspect
import json
import logging
import os
from typing import Optional

import datasets
import torch
from datasets import Dataset
from packaging import version
from torch.utils.data import DataLoader
from transformers import Trainer, is_datasets_available
from transformers.trainer_pt_utils import IterableDatasetShard

logger = logging.getLogger(__name__)


class MRCTrainer(Trainer):
    def __init__(self, *args, eval_examples=None, eval_dataset=None, post_process_function=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.eval_examples = eval_examples
        self.eval_dataset = eval_dataset
        self.post_process_function = post_process_function

    def _remove_unused_columns(self, dataset: "datasets.Dataset", description: Optional[str] = None):
        if not self.args.remove_unused_columns:
            return dataset
        if self._signature_columns is None:
            # Inspect model and task head forward signature to keep only the arguments it accepts.
            model_signature = inspect.signature(self.model.forward)
            task_head_signature = inspect.signature(self.model.task_head.forward)

            signature_columns = set(model_signature.parameters.keys())
            signature_columns |= task_head_signature.parameters.keys()
            signature_columns -= {'kwargs'}

            # Labels may be named label or label_ids, the default data collator handles that.
            signature_columns |= {"label", "label_ids"}

            self._signature_columns = list(signature_columns)

        columns = [k for k in self._signature_columns if k in dataset.column_names]
        ignored_columns = list(set(dataset.column_names) - set(self._signature_columns))
        if len(ignored_columns) > 0:
            dset_description = "" if description is None else f"in the {description} set "
            logger.info(
                f"The following columns {dset_description} don't have a corresponding argument in "
                f"`{self.model.__class__.__name__}.forward` and have been ignored: {', '.join(ignored_columns)}."
            )

        if version.parse(datasets.__version__) < version.parse("1.4.0"):
            dataset.set_format(
                type=dataset.format["type"], columns=columns, format_kwargs=dataset.format["format_kwargs"]
            )
            return dataset
        else:
            return dataset.remove_columns(ignored_columns)

    def get_train_dataloader(self) -> DataLoader:
        """
        Returns the training [`~torch.utils.data.DataLoader`].

        Will use no sampler if `self.train_dataset` does not implement `__len__`, a random sampler (adapted
        to distributed training if necessary) otherwise.

        Subclass and override this method if you want to inject some custom behavior.
        """
        if self.train_dataset is None:
            raise ValueError("Trainer: training requires a train_dataset.")

        train_dataset = self.train_dataset
        if is_datasets_available() and isinstance(train_dataset, datasets.Dataset):
            train_dataset = self._remove_unused_columns(train_dataset, description="training")

        if isinstance(train_dataset, torch.utils.data.IterableDataset):
            if self.args.world_size > 1:
                train_dataset = IterableDatasetShard(
                    train_dataset,
                    batch_size=self.args.train_batch_size,
                    drop_last=self.args.dataloader_drop_last,
                    num_processes=self.args.world_size,
                    process_index=self.args.process_index,
                )

            return DataLoader(
                train_dataset,
                batch_size=self.args.per_device_train_batch_size,
                collate_fn=self.data_collator,
                num_workers=self.args.dataloader_num_workers,
                pin_memory=self.args.dataloader_pin_memory,
            )

        train_sampler = self._get_train_sampler()

        return DataLoader(
            train_dataset,
            batch_size=self.args.train_batch_size,
            sampler=train_sampler,
            collate_fn=self.data_collator,
            drop_last=self.args.dataloader_drop_last,
            num_workers=self.args.dataloader_num_workers,
            pin_memory=self.args.dataloader_pin_memory,
        )

    def get_eval_dataloader(self, eval_dataset: Optional[Dataset] = None) -> DataLoader:
        """
        Returns the evaluation [`~torch.utils.data.DataLoader`].

        Subclass and override this method if you want to inject some custom behavior.

        Args:
            eval_dataset (`torch.utils.data.Dataset`, *optional*)
            If provided, will override `self.eval_dataset`. If it is an `datasets.Dataset`, columns not
                accepted by the `model.forward()` method are automatically removed. It must implement `__len__`.

        """

        if eval_dataset is None and self.eval_dataset is None:
            raise ValueError("Trainer: evaluation requires an eval_dataset.")
        eval_dataset = eval_dataset if eval_dataset is not None else self.eval_dataset

        if is_datasets_available() and isinstance(eval_dataset, datasets.Dataset):
            eval_dataset = self._remove_unused_columns(eval_dataset, description="evaluation")

        if isinstance(eval_dataset, torch.utils.data.IterableDataset):
            if self.args.world_size > 1:
                eval_dataset = IterableDatasetShard(
                    eval_dataset,
                    batch_size=self.args.per_device_eval_batch_size,
                    drop_last=self.args.dataloader_drop_last,
                    num_processes=self.args.world_size,
                    process_index=self.args.process_index,
                )
            return DataLoader(
                eval_dataset,
                batch_size=self.args.eval_batch_size,
                collate_fn=self.data_collator,
                num_workers=self.args.dataloader_num_workers,
                pin_memory=self.args.dataloader_pin_memory,
            )

        eval_sampler = self._get_eval_sampler(eval_dataset)

        return DataLoader(
            eval_dataset,
            sampler=eval_sampler,
            batch_size=self.args.eval_batch_size,
            collate_fn=self.data_collator,
            drop_last=self.args.dataloader_drop_last,
            num_workers=self.args.dataloader_num_workers,
            pin_memory=self.args.dataloader_pin_memory,
        )

    # TODO
    # def get_test_dataloader(self, test_dataset: Dataset) -> DataLoader:
    #     """
    #     Returns the test [`~torch.utils.data.DataLoader`].
    #
    #     Subclass and override this method if you want to inject some custom behavior.
    #
    #     Args:
    #         test_dataset (`torch.utils.data.Dataset`, *optional*):
    #             The test dataset to use. If it is an `datasets.Dataset`, columns not accepted by the
    #             `model.forward()` method are automatically removed. It must implement `__len__`.
    #     """
    #     if is_datasets_available() and isinstance(test_dataset, datasets.Dataset):
    #         test_dataset = self._remove_unused_columns(test_dataset, description="test")
    #
    #     if isinstance(test_dataset, torch.utils.data.IterableDataset):
    #         if self.args.world_size > 1:
    #             test_dataset = IterableDatasetShard(
    #                 test_dataset,
    #                 batch_size=self.args.eval_batch_size,
    #                 drop_last=self.args.dataloader_drop_last,
    #                 num_processes=self.args.world_size,
    #                 process_index=self.args.process_index,
    #             )
    #         return DataLoader(
    #             test_dataset,
    #             batch_size=self.args.eval_batch_size,
    #             collate_fn=self.data_collator,
    #             num_workers=self.args.dataloader_num_workers,
    #             pin_memory=self.args.dataloader_pin_memory,
    #         )
    #
    #     test_sampler = self._get_eval_sampler(test_dataset)
    #
    #     # We use the same batch_size as for eval.
    #     return DataLoader(
    #         test_dataset,
    #         sampler=test_sampler,
    #         batch_size=self.args.eval_batch_size,
    #         collate_fn=self.data_collator,
    #         drop_last=self.args.dataloader_drop_last,
    #         pin_memory=self.args.dataloader_pin_memory,
    #     )

    def evaluate(self, eval_dataset=None, eval_examples=None, ignore_keys=None, metric_key_prefix: str = "eval"):
        eval_dataset = self.eval_dataset if eval_dataset is None else eval_dataset
        eval_dataloader = self.get_eval_dataloader(eval_dataset)
        eval_examples = self.eval_examples if eval_examples is None else eval_examples

        # # Temporarily disable metric computation, we will do it in the loop here.
        compute_metrics = self.compute_metrics
        self.compute_metrics = None
        eval_loop = self.prediction_loop if self.args.use_legacy_prediction_loop else self.evaluation_loop
        try:
            output = eval_loop(
                eval_dataloader,
                description="Evaluation",
                # No point gathering the predictions if there are no metrics, otherwise we defer to
                # self.args.prediction_loss_only
                # gather predictions if running in eval mode
                prediction_loss_only=self.args.prediction_loss_only, #True if compute_metrics is None else None,
                ignore_keys=ignore_keys,
            )
        finally:
            self.compute_metrics = compute_metrics
        

        if self.post_process_function is not None:
            eval_preds = self.post_process_function(eval_examples, eval_dataset, output.predictions)

            # TODO: return eval_preds and metrics, write save function for preds
            with open(os.path.join(self.args.output_dir, 'eval_predictions.json'), 'w') as f:
                json.dump(eval_preds.predictions, f, indent=4)
            with open(os.path.join(self.args.output_dir, 'eval_predictions_processed.json'), 'w') as f:
                json.dump(eval_preds.processed_predictions, f, indent=4)
        if self.post_process_function is not None and self.compute_metrics is not None:
            metrics = self.compute_metrics(eval_preds)

            # Prefix all keys with metric_key_prefix + '_'
            for key in list(metrics.keys()):
                if not key.startswith(f"{metric_key_prefix}_"):
                    metrics[f"{metric_key_prefix}_{key}"] = metrics.pop(key)

            self.log(metrics)
        else:
            metrics = {}

        # if self.args.tpu_metrics_debug or self.args.debug:
        #     # tpu-comment: Logging debug metrics for PyTorch/XLA (compile, execute times, ops, etc.)
        #     xm.master_print(met.metrics_report())

        self.control = self.callback_handler.on_evaluate(self.args, self.state, self.control, metrics)
        return metrics
