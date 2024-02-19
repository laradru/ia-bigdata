# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~#
# Core module for training a deep learning model for computer vision tasks                                            #
# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~#

import numpy as np
import torch
from pycocotools.coco import COCO
from pycocotools.cocoeval import COCOeval
from torch.optim import Optimizer
from torch.utils.data import DataLoader
from tqdm import tqdm

from src.dataset.annotations_coco import COCOAnnotations
from src.dataset.dataset_utils import extract_bbox_segmentation
from src.training.tensorboard import TrainingRecorder


class SupervisedTrainer:
    def __init__(self, device: str, model: torch.nn.Module, recorder: TrainingRecorder = None, seed: int = None):
        """Class constructor. Initializes the trainer module for supervised learning.

        Args:
            device (str): The device to use for training (e.g., 'cuda' or 'cpu').
            model (torch.nn.Module): The model to be trained.
            recorder (TrainingRecorder, optional): A training recorder to track training progress. Defaults to None.
            seed (int, optional): The seed to use for reproducibility. Defaults to None.
        """

        self.main_metric = "loss_mask"
        self.device = device
        self.model = model
        self.recorder = recorder  # Tensorboard recorder to track training progress.
        self.best_loss = 1e20  # Set to a large value, so that the first validation loss is always better.

        if seed is not None:
            torch.manual_seed(seed)  # CPU
            if self.device == "cuda":
                torch.cuda.manual_seed_all(seed)  # GPU

        self.model.to(self.device)  # Load model in the GPU

    def train(self, dataset: torch.utils.data.DataLoader, optimizer: Optimizer) -> dict:
        """Trains the model on a given dataset.

        Args:
            dataset (torch.utils.data.DataLoader): The dataset to train the model on.
            optimizer (torch.optim.Optimizer): The optimizer used for training.

        Returns:
            float: The average training loss over all batches.
        """

        loss_train = {
            "loss_classifier": 0,
            "loss_box_reg": 0,
            "loss_mask": 0,
            "loss_objectness": 0,
            "loss_rpn_box_reg": 0,
        }

        total_images = dataset.sampler.num_samples
        prog_bar = tqdm(total=total_images, ascii=True, unit="images", colour="green", desc="Training Phase")

        # Set module status to training. Implemented in torch.nn.Module
        self.model.train()

        with torch.set_grad_enabled(True):
            for batch in dataset:
                x_pred, y_true = batch
                x_pred = [x.to(self.device) for x in x_pred]
                y_true = [{key: yt[key].to(self.device) for key in yt.keys()} for yt in y_true]

                # Zero gradients for each batch
                optimizer.zero_grad()

                # Predict
                losses = self.model(x_pred, y_true)

                # Loss computation and weights correction
                loss = sum(loss for loss in losses.values())
                loss.backward()  # backpropagation
                optimizer.step()

                loss_train = {key: loss_train[key] + value.item() for key, value in losses.items()}

                prog_bar.n += len(x_pred)
                prog_bar.refresh()

        prog_bar.close()
        return {key: loss_train[key] / total_images for key in loss_train.keys()}

    def evaluate(self, dataset: torch.utils.data.DataLoader) -> dict:
        """Calculate the evaluation loss on the given dataset.

        Args:
            dataset (torch.utils.data.DataLoader): The dataset to evaluate the model on.

        Returns:
            float: The average validation loss.
        """

        loss_valid = {
            "loss_classifier": 0,
            "loss_box_reg": 0,
            "loss_mask": 0,
            "loss_objectness": 0,
            "loss_rpn_box_reg": 0,
        }

        total_images = dataset.sampler.num_samples
        prog_bar = tqdm(total=total_images, ascii=True, unit="images", colour="red", desc="Validation Phase")

        # Set module status to train because we want to get the validation loss.
        # self.model.eval() gives us predictions as model output.
        self.model.train()

        with torch.no_grad():
            for batch in dataset:
                x_pred, y_true = batch
                x_pred = [x.to(self.device) for x in x_pred]
                y_true = [{key: yt[key].to(self.device) for key in yt.keys()} for yt in y_true]

                # Predict
                losses = self.model(x_pred, y_true)
                loss_valid = {key: loss_valid[key] + value.item() for key, value in losses.items()}

                prog_bar.n += len(x_pred)
                prog_bar.refresh()

        prog_bar.close()
        return {key: loss_valid[key] / total_images for key in loss_valid.keys()}

    def coco_eval(self, dataset: torch.utils.data.DataLoader) -> None:
        """Performs COCO evaluation using ground truth and predicted annotations.

        Args:
            dataset (torch.utils.data.DataLoader): The dataset containing ground truth annotations.

        Returns:
            None
        """

        gt_annotations = COCOAnnotations.from_dict(dataset.dataset.tree.data)
        pred_annotations = COCOAnnotations.from_dict(dataset.dataset.tree.data)
        pred_annotations.data["annotations"] = []
        annotation_id = 1

        total_images = dataset.sampler.num_samples
        prog_bar = tqdm(total=total_images, ascii=True, unit="images", colour="yellow", desc="COCO Evaluation Phase")

        self.model.eval()
        with torch.no_grad():
            for sample in pred_annotations.data["images"]:
                image_id = sample["id"]
                image, __ = dataset.dataset[image_id - 1]

                pred = self.model([image.to(self.device)], None)[0]

                for box, label, mask, score in zip(pred["boxes"], pred["labels"], pred["masks"], pred["scores"]):
                    mask = mask.cpu().numpy().astype(np.uint8)
                    mask = np.transpose(mask, (1, 2, 0))

                    if mask.sum() == 0:
                        continue

                    __, segmentation = extract_bbox_segmentation(mask)

                    pred_annotations.add_annotation_instance(
                        id=annotation_id,
                        image_id=image_id,
                        category_id=label.item(),
                        bbox=box.tolist(),
                        segmentation=segmentation,
                        score=score.item(),
                        area=len(mask[mask > 0]),
                        iscrowd=0,
                    )
                    annotation_id += 1

                prog_bar.n += 1
                prog_bar.refresh()
        prog_bar.close()

        coco_ground_truth = COCO()
        coco_ground_truth.dataset = gt_annotations.data
        coco_ground_truth.createIndex()

        coco_detection = COCO()
        coco_detection.dataset = pred_annotations.data
        coco_detection.createIndex()

        evaluator = COCOeval(coco_ground_truth, coco_detection, iouType="segm")
        evaluator.evaluate()
        evaluator.accumulate()
        evaluator.summarize()

    def fit(self, training_data: DataLoader, validation_data: DataLoader, optimizer: Optimizer, epochs: int, **kwargs):
        """Fits the model to the training dataset and validates it on the validation dataset for a
        specified number of epochs.

        Parameters:
            training_data (DataLoader): The dataset used for training.
            validation_data (DataLoader): The dataset used for validation.
            optimizer (Optimizer): The optimizer used for training.
            epochs (int): The number of epochs to train the model.
        """

        coco_eval_frequency = kwargs.get("coco_eval_frequency", None)

        for epoch in range(epochs):
            print(f"Epoch {epoch}")

            loss_training = self.train(training_data, optimizer)
            loss_validation = self.evaluate(validation_data)

            if coco_eval_frequency is not None and epoch % coco_eval_frequency == 0:
                self.coco_eval(validation_data)

            print(f"Loss training: {loss_training}")
            print(f"Loss validation: {loss_validation}")

            if self.recorder:
                self.recorder.record_scalars("training loss", loss_training, epoch)
                self.recorder.record_scalars("validation loss", loss_validation, epoch)

            # Save checkpoint.
            if loss_validation[self.main_metric] < self.best_loss:
                self.best_loss = loss_validation[self.main_metric]
                self.model.save()

        if self.recorder:
            self.recorder.close()
