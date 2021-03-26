{% block imports %}
from ignite.distributed import auto_dataloader
from torch.utils.data import Dataset
from torchvision import transforms as T
from torchvision.datasets import CIFAR10

{% endblock %}

{% block datasets %}
def get_datasets(root: str):
    train_transforms = T.Compose([
        T.Pad(4),
        T.RandomCrop(32, fill=128),
        T.RandomHorizontalFlip(),
        T.ToTensor(),
        T.Normalize((0.485, 0.456, 0.406), (0.229, 0.224, 0.225)),
    ])
    eval_transforms = T.Compose([
        T.ToTensor(),
        T.Normalize((0.485, 0.456, 0.406), (0.229, 0.224, 0.225)),
    ])
    train_dataset = CIFAR10(root=root, train=True, download=True, transform=train_transforms)
    eval_dataset = CIFAR10(root=root, train=False, download=True, transform=eval_transforms)
    return train_dataset, eval_dataset
{% endblock %}


{% block data_loaders %}
def get_data_loaders(
    train_dataset: Dataset,
    eval_dataset: Dataset,
    train_batch_size: int,
    eval_batch_size: int,
    num_workers: int,
):
    train_dataloader = auto_dataloader(
        train_dataset,
        batch_size=train_batch_size,
        shuffle=True,
        num_workers=num_workers,
    )
    eval_dataloader = auto_dataloader(
        eval_dataset,
        batch_size=eval_batch_size,
        shuffle=False,
        num_workers=num_workers
    )
    return train_dataloader, eval_dataloader
{% endblock %}