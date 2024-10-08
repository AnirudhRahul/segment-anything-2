# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.

# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

import os
import warnings
from threading import Thread

import numpy as np
import torch
from PIL import Image
from tqdm import tqdm
from concurrent.futures import ThreadPoolExecutor, as_completed
import queue


def get_sdpa_settings():
    if torch.cuda.is_available():
        old_gpu = torch.cuda.get_device_properties(0).major < 7
        # only use Flash Attention on Ampere (8.0) or newer GPUs
        use_flash_attn = torch.cuda.get_device_properties(0).major >= 8
        if not use_flash_attn:
            warnings.warn(
                "Flash Attention is disabled as it requires a GPU with Ampere (8.0) CUDA capability.",
                category=UserWarning,
                stacklevel=2,
            )
        # keep math kernel for PyTorch versions before 2.2 (Flash Attention v2 is only
        # available on PyTorch 2.2+, while Flash Attention v1 cannot handle all cases)
        pytorch_version = tuple(int(v) for v in torch.__version__.split(".")[:2])
        if pytorch_version < (2, 2):
            warnings.warn(
                f"You are using PyTorch {torch.__version__} without Flash Attention v2 support. "
                "Consider upgrading to PyTorch 2.2+ for Flash Attention v2 (which could be faster).",
                category=UserWarning,
                stacklevel=2,
            )
        math_kernel_on = pytorch_version < (2, 2) or not use_flash_attn
    else:
        old_gpu = True
        use_flash_attn = False
        math_kernel_on = True
    print("SPDA settings: ", old_gpu, use_flash_attn, math_kernel_on)
    return old_gpu, use_flash_attn, math_kernel_on


def get_connected_components(mask):
    """
    Get the connected components (8-connectivity) of binary masks of shape (N, 1, H, W).

    Inputs:
    - mask: A binary mask tensor of shape (N, 1, H, W), where 1 is foreground and 0 is
            background.

    Outputs:
    - labels: A tensor of shape (N, 1, H, W) containing the connected component labels
              for foreground pixels and 0 for background pixels.
    - counts: A tensor of shape (N, 1, H, W) containing the area of the connected
              components for foreground pixels and 0 for background pixels.
    """
    from sam2 import _C

    return _C.get_connected_componnets(mask.to(torch.uint8).contiguous())


def mask_to_box(masks: torch.Tensor):
    """
    compute bounding box given an input mask

    Inputs:
    - masks: [B, 1, H, W] masks, dtype=torch.Tensor

    Returns:
    - box_coords: [B, 1, 4], contains (x, y) coordinates of top left and bottom right box corners, dtype=torch.Tensor
    """
    B, _, h, w = masks.shape
    device = masks.device
    xs = torch.arange(w, device=device, dtype=torch.int32)
    ys = torch.arange(h, device=device, dtype=torch.int32)
    grid_xs, grid_ys = torch.meshgrid(xs, ys, indexing="xy")
    grid_xs = grid_xs[None, None, ...].expand(B, 1, h, w)
    grid_ys = grid_ys[None, None, ...].expand(B, 1, h, w)
    min_xs, _ = torch.min(torch.where(masks, grid_xs, w).flatten(-2), dim=-1)
    max_xs, _ = torch.max(torch.where(masks, grid_xs, -1).flatten(-2), dim=-1)
    min_ys, _ = torch.min(torch.where(masks, grid_ys, h).flatten(-2), dim=-1)
    max_ys, _ = torch.max(torch.where(masks, grid_ys, -1).flatten(-2), dim=-1)
    bbox_coords = torch.stack((min_xs, min_ys, max_xs, max_ys), dim=-1)

    return bbox_coords


import cv2
import time
import torch
import numpy as np

def _load_img_as_tensor(img_path, image_size):
    # # Create a new directory for resized images
    # resized_dir = os.path.join(os.path.dirname(img_path), "resized")
    # os.makedirs(resized_dir, exist_ok=True)


    

    # Read image using OpenCV
    img_cv = cv2.imread(img_path)
    # if img_cv is None:
    #     raise RuntimeError(f"Failed to load image: {img_path}")
    

    # img_cv = cv2.resize(img_cv, (image_size, image_size))
    # base_name = os.path.basename(img_path)
    # new_img_path = os.path.join(resized_dir, f"resized_{base_name}")
    # cv2.imwrite(new_img_path, img_cv)
    
    # Convert BGR to RGB
    img_cv = cv2.cvtColor(img_cv, cv2.COLOR_BGR2RGB)
    
    # # Convert to float and normalize
    # img_np = img_cv.astype(np.float32) / 255.0
    
    # Convert to tensor and rearrange dimensions
    img = torch.from_numpy(img_cv).permute(2, 0, 1) / 255.0
    
    # Get original image dimensions
    video_height, video_width = img_cv.shape[:2]
    
    return img, video_height, video_width


import threading
import time
from threading import Thread
from collections import OrderedDict

# Least Recently Added Cache
class LRACache(OrderedDict):
    'Limit size, evicting the least recently looked-up key when full'

    def __init__(self, maxsize, *args, **kwds):
        self.maxsize = maxsize
        super().__init__(*args, **kwds)

    def __getitem__(self, key):
        value = super().__getitem__(key)
        return value

    def __setitem__(self, key, value):
        super().__setitem__(key, value)
        if len(self) > self.maxsize:
            oldest = next(iter(self))
            del self[oldest]


class TaskQueue:
    def __init__(self, num_workers=3):
        self.tasks = queue.Queue()
        self.workers = []
        for _ in range(num_workers):
            worker = threading.Thread(target=self._worker)
            worker.daemon = True  # Set thread as daemon
            worker.start()
            self.workers.append(worker)

    def add_task(self, task):
        self.tasks.put(task)

    def _worker(self):
        while True:
            try:
                frame, func = self.tasks.get(timeout=1)  # Add timeout to allow checking for program exit
                func(frame)
                self.tasks.task_done()
            except queue.Empty:
                continue


class AsyncVideoFrameLoader:
    """
    A list of video frames to be loaded asynchronously without blocking session start.
    """

    def __init__(
        self,
        img_paths,
        image_size,
        offload_video_to_cpu,
        img_mean,
        img_std,
        compute_device,
        cache_size=200,
        start_frame=0,
    ):
        self.img_paths = img_paths
        self.image_size = image_size
        self.offload_video_to_cpu = offload_video_to_cpu
        self.img_mean = img_mean
        self.img_std = img_std
        self.compute_device = compute_device
        self.cache_size = cache_size
        self.images = LRACache(maxsize=self.cache_size)  # Map of frame index to cached image
        self.exception = None
        self.video_height = None
        self.video_width = None
        self.last_accessed_frame = start_frame
        self.task_queue = TaskQueue(num_workers=10)  # Use TaskQueue instead of ThreadPoolExecutor

        # Load the first frame
        self.__getitem__(start_frame)

        # Start async loading thread
        self.thread = Thread(target=self._load_frames, daemon=True)
        self.thread.start()

    def _load_frames(self):
        try:
            K = int(self.cache_size * 0.75)
            with tqdm(total=len(self.img_paths), initial=self.last_accessed_frame, desc="Loading frames") as pbar:
                just_ran = None
                while True:
                    current_frame = self.last_accessed_frame
                    # print("current_frame: ", current_frame)
                    end_frame = min(current_frame + K, len(self.img_paths))

                    for frame in range(current_frame, end_frame):
                        if frame not in self.images:
                            self.task_queue.add_task((frame, self._load_frame))
                    
                    pbar.n = current_frame
                    pbar.refresh()

                    just_ran = current_frame
                    
                    while just_ran == self.last_accessed_frame:
                        time.sleep(0.1)  # Wait if we're too far ahead

        except Exception as e:
            self.exception = e


    def _load_frame(self, index):        
        img, video_height, video_width = _load_img_as_tensor(
            self.img_paths[index], self.image_size
        )
        self.video_height = video_height
        self.video_width = video_width
        img -= self.img_mean
        img /= self.img_std
        if not self.offload_video_to_cpu:
            img = img.to(self.compute_device, non_blocking=True)
        self.images[index] = img


    def __getitem__(self, index):
        if self.exception:
            raise RuntimeError("Failure in frame loading thread") from self.exception
        
        self.last_accessed_frame = index
        if index not in self.images:
            print(f"Cache miss for frame {index}")
            self._load_frame(index)
            return self.images[index]
        else:
            res = self.images[index]
            del self.images[index]
            return res

    def __len__(self):
        return len(self.img_paths)


def load_video_frames(
    video_path,
    image_size,
    offload_video_to_cpu,
    img_mean=(0.485, 0.456, 0.406),
    img_std=(0.229, 0.224, 0.225),
    async_loading_frames=False,
    compute_device=torch.device("cuda"),
):
    """
    Load the video frames from a directory of JPEG files ("<frame_index>.jpg" format).

    The frames are resized to image_size x image_size and are loaded to GPU if
    `offload_video_to_cpu` is `False` and to CPU if `offload_video_to_cpu` is `True`.

    You can load a frame asynchronously by setting `async_loading_frames` to `True`.
    """
    if isinstance(video_path, str) and os.path.isdir(video_path):
        jpg_folder = video_path
    else:
        raise NotImplementedError(
            "Only JPEG frames are supported at this moment. For video files, you may use "
            "ffmpeg (https://ffmpeg.org/) to extract frames into a folder of JPEG files, such as \n"
            "```\n"
            "ffmpeg -i <your_video>.mp4 -q:v 2 -start_number 0 <output_dir>/'%05d.jpg'\n"
            "```\n"
            "where `-q:v` generates high-quality JPEG frames and `-start_number 0` asks "
            "ffmpeg to start the JPEG file from 00000.jpg."
        )

    frame_names = [
        p
        for p in os.listdir(jpg_folder)
        if os.path.splitext(p)[-1] in [".jpg", ".jpeg", ".JPG", ".JPEG"]
    ]
    frame_names.sort(key=lambda p: int(os.path.splitext(p)[0]))
    num_frames = len(frame_names)
    if num_frames == 0:
        raise RuntimeError(f"no images found in {jpg_folder}")
    img_paths = [os.path.join(jpg_folder, frame_name) for frame_name in frame_names]
    img_mean = torch.tensor(img_mean, dtype=torch.float32)[:, None, None]
    img_std = torch.tensor(img_std, dtype=torch.float32)[:, None, None]

    if async_loading_frames:
        lazy_images = AsyncVideoFrameLoader(
            img_paths,
            image_size,
            offload_video_to_cpu,
            img_mean,
            img_std,
            compute_device,
        )
        return lazy_images, lazy_images.video_height, lazy_images.video_width


    images = torch.zeros(num_frames, 3, image_size, image_size, dtype=torch.float32)
    video_height, video_width = None, None

    def load_image(args):
        n, img_path = args
        img, height, width = _load_img_as_tensor(img_path, image_size)
        return n, img, height, width

    with ThreadPoolExecutor() as executor:
        futures = [executor.submit(load_image, (n, img_path)) for n, img_path in enumerate(img_paths)]
        
        for future in tqdm(as_completed(futures), total=len(futures), desc="frame loading (JPEG)"):
            n, img, height, width = future.result()
            images[n] = img
            if video_height is None:
                video_height, video_width = height, width

    if not offload_video_to_cpu:
        images = images.to(compute_device)
        img_mean = img_mean.to(compute_device)
        img_std = img_std.to(compute_device)
    # normalize by mean and std
    images -= img_mean
    images /= img_std
    return images, video_height, video_width


def fill_holes_in_mask_scores(mask, max_area):
    """
    A post processor to fill small holes in mask scores with area under `max_area`.
    """
    # Holes are those connected components in background with area <= self.max_area
    # (background regions are those with mask scores <= 0)
    assert max_area > 0, "max_area must be positive"

    input_mask = mask
    try:
        labels, areas = get_connected_components(mask <= 0)
        is_hole = (labels > 0) & (areas <= max_area)
        # We fill holes with a small positive mask score (0.1) to change them to foreground.
        mask = torch.where(is_hole, 0.1, mask)
    except Exception as e:
        # Skip the post-processing step on removing small holes if the CUDA kernel fails
        warnings.warn(
            f"{e}\n\nSkipping the post-processing step due to the error above. You can "
            "still use SAM 2 and it's OK to ignore the error above, although some post-processing "
            "functionality may be limited (which doesn't affect the results in most cases; see "
            "https://github.com/facebookresearch/segment-anything-2/blob/main/INSTALL.md).",
            category=UserWarning,
            stacklevel=2,
        )
        mask = input_mask

    return mask


def concat_points(old_point_inputs, new_points, new_labels):
    """Add new points and labels to previous point inputs (add at the end)."""
    if old_point_inputs is None:
        points, labels = new_points, new_labels
    else:
        points = torch.cat([old_point_inputs["point_coords"], new_points], dim=1)
        labels = torch.cat([old_point_inputs["point_labels"], new_labels], dim=1)

    return {"point_coords": points, "point_labels": labels}