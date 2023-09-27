import math
import os
import random
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np
import torch
import tyro
from diff_rast.project_gaussians import ProjectGaussians
from diff_rast.rasterize import RasterizeGaussians
from PIL import Image
from torch import Tensor, optim


@dataclass
class Camera:
    viewmat: Tensor
    name: str

    def to(self, device):
        return Camera(self.viewmat.to(device), self.name)

@dataclass
class PosedImage:
    image: Tensor
    camera: Camera

    def to(self, device):
        return PosedImage(self.image.to(device), self.camera.to(device))


class SimpleTrainer:
    """Trains random gaussians to fit an image."""

    def __init__(
        self,
        # gt_image: Tensor,
        data: list[PosedImage],
        debug_cameras: Optional[list[Camera]] = None,
        num_points: int = 2000,
    ):
        debug_cameras = debug_cameras or []
        if len(data) == 0:
            raise ValueError("Must have at least one image")
        self.H, self.W = data[0].image.shape[0], data[0].image.shape[1]
        if not all(image.shape == (self.H, self.W) for image in data):
            raise ValueError("All images must have the same shape")
        self.device = torch.device("cuda:0")
        # self.gt_image = gt_image.to(device=self.device)
        self.num_points = num_points
        self.data = [posed_image.to(self.device) for posed_image in data]
        self.debug_cameras = [camera.to(self.device) for camera in debug_cameras]

        BLOCK_X, BLOCK_Y = 16, 16
        fov_x = math.pi / 2.0
        self.focal = 0.5 * float(self.W) / math.tan(0.5 * fov_x)
        self.tile_bounds = (
            (self.W + BLOCK_X - 1) // BLOCK_X,
            (self.H + BLOCK_Y - 1) // BLOCK_Y,
            1,
        )
        self.img_size = torch.tensor([self.W, self.H, 1], device=self.device)
        self.block = torch.tensor([BLOCK_X, BLOCK_Y, 1], device=self.device)

        self._init_gaussians()

    def _init_gaussians(self):
        """Random gaussians"""
        self.means = torch.empty((self.num_points, 3), device=self.device)
        self.scales = torch.empty((self.num_points, 3), device=self.device)
        self.quats = torch.empty((self.num_points, 4), device=self.device)
        self.rgbs = torch.ones((self.num_points, 3), device=self.device)
        self.opacities = torch.ones(self.num_points, device=self.device)
        bd = 2
        for i in range(self.num_points):
            self.means[i] = torch.tensor(
                [
                    bd * (random.random() - 0.5),
                    bd * (random.random() - 0.5),
                    bd * (random.random() - 0.5),
                ],
                device=self.device,
            )
            self.scales[i] = torch.tensor(
                [random.random(), random.random(), random.random()], device=self.device
            )
            self.rgbs[i] = torch.tensor(
                [random.random(), random.random(), random.random()], device=self.device
            )
            u = random.random()
            v = random.random()
            w = random.random()
            self.quats[i] = torch.tensor(
                [
                    math.sqrt(1.0 - u) * math.sin(2.0 * math.pi * v),
                    math.sqrt(1.0 - u) * math.cos(2.0 * math.pi * v),
                    math.sqrt(u) * math.sin(2.0 * math.pi * w),
                    math.sqrt(u) * math.cos(2.0 * math.pi * w),
                ],
                device=self.device,
            )

        # self.viewmat = torch.tensor(
        #     [
        #         [1.0, 0.0, 0.0, 0.0],
        #         [0.0, 1.0, 0.0, 0.0],
        #         [0.0, 0.0, 1.0, 8.0],
        #         [0.0, 0.0, 0.0, 1.0],
        #     ],
        #     device=self.device,
        # )
        # self.viewmat2 = torch.tensor(
        #     [
        #         [ 0.,  0.,  1.,  8.],
        #         [ 0.,  1.,  0.,  0.],
        #         [-1.,  0.,  0.,  0.],
        #         [ 0.,  0.,  0.,  1.]
        #     ]
        # )
        # theta = np.pi / 4
        # self.viewmat3 = torch.tensor(
        #     [
        #         [ np.cos(theta),  0.,  np.sin(theta),  4.],
        #         [ 0.,             1.,  0.,             0.],
        #         [-np.sin(theta),  0.,  np.cos(theta),  4.],
        #         [ 0.,             0.,  0.,             1.]
        #     ]
        # )
        self.means.requires_grad = True
        self.scales.requires_grad = False
        self.quats.requires_grad = False
        self.rgbs.requires_grad = True
        self.opacities.requires_grad = False
        self.viewmat.requires_grad = False

    def _save_img(self, image, image_path):
        if torch.is_tensor(image):
            image = image.detach().cpu().numpy() * 255
            image = image.astype(np.uint8)
        if not Path(os.path.dirname(image_path)).exists():
            Path(os.path.dirname(image_path)).mkdir()
        im = Image.fromarray(image)
        print("saving to: ", image_path)
        im.save(image_path)

    def train(self, iterations: int = 1000, lr: float = 0.01, save_imgs: bool = True):
        optimizer = optim.Adam(
            [self.rgbs, self.means, self.scales, self.opacities, self.quats], lr
        )  # try training self.opacities/scales etc.
        mse_loss = torch.nn.MSELoss()
        name_to_frames = defaultdict(list)
        # frames = []

        def _compute_out_image(viewmat):
            xys, depths, radii, conics, num_tiles_hit = ProjectGaussians.apply(
                self.means,
                self.scales,
                1,
                self.quats,
                viewmat,
                viewmat,
                self.focal,
                self.focal,
                self.H,
                self.W,
                self.tile_bounds,
            )

            out_img = RasterizeGaussians.apply(xys, depths, radii, conics, num_tiles_hit, torch.sigmoid(self.rgbs),
                                               torch.sigmoid(self.opacities), self.H, self.W)
            return out_img

        for iter in range(iterations):
            # out_img = _compute_out_image(self.viewmat)
            # out_img2 = _compute_out_image(self.viewmat2)
            # out_img3 = _compute_out_image(self.viewmat3)
            # loss = mse_loss(out_img, self.gt_image) + mse_loss(out_img2, self.gt_image)
            name_to_out_img = {
                camera.name: _compute_out_image(camera.viewmat)
                for camera in [c for c in self.data] + self.debug_cameras
            }
            loss = sum([
                mse_loss(name_to_out_img[camera.name], image)
                for image, camera in self.data
            ])
            optimizer.zero_grad()
            loss.backward()
            optimizer.step()
            print(f"Iteration {iter + 1}/{iterations}, Loss: {loss.item()}")
            print("RGB MIN", self.rgbs.min().item(), "RGB MAX", self.rgbs.max().item())
            print(
                "OPACITY MIN",
                self.opacities.min().item(),
                "OPACITY MAX",
                self.opacities.max().item(),
            )
            # same line but for out_img
            print(
                "OUT_IMG MIN", out_img.min().item(), "OUT_IMG MAX", out_img.max().item()
            )
            if save_imgs and iter % (iterations // 25) == 0:
                # frames.append((out_img.detach().cpu().numpy() * 255).astype(np.uint8))
                for name, out_img in name_to_out_img.items():
                    name_to_frames[name].append((out_img.detach().cpu().numpy() * 255).astype(np.uint8))
                # name_to_frames["right"].append((out_img2.detach().cpu().numpy() * 255).astype(np.uint8))
                # name_to_frames["skew"].append((out_img3.detach().cpu().numpy() * 255).astype(np.uint8))
        if save_imgs:
            #save them as a gif with PIL
            for name, frames in name_to_frames.items():
                frames = [Image.fromarray(frame) for frame in frames]
                # save_dir = os.getcwd() + "/renders"
                save_path = Path.cwd() / "renders" / f"{name}.gif"
                # save_path = Path(os.getcwd() + f"/renders/{name}.gif")
                if not save_path.parent.exists():
                    save_path.parent.mkdir()
                frames[0].save(str(save_path), save_all=True, append_images=frames[1:], optimize=False, duration=5, loop=0)


def main(height: int = 256, width: int = 256) -> None:
    gt_image = torch.ones((height, width, 3)) * 1.0
    # make top left and bottom right red,blue
    gt_image[: height // 2, : width // 2, :] = torch.tensor([1.0, 0.0, 0.0])
    gt_image[height // 2 :, width // 2 :, :] = torch.tensor([0.0, 0.0, 1.0])

    input = [
        PosedImage(
            image=gt_image,
            camera=Camera(
                viewmat=torch.tensor(
                    [
                        [1.0, 0.0, 0.0, 0.0],
                        [0.0, 1.0, 0.0, 0.0],
                        [0.0, 0.0, 1.0, 8.0],
                        [0.0, 0.0, 0.0, 1.0],
                    ],
                ),
                name="front",
            ),
        ),
        # PosedImage(
        #     image=gt_image,
        #     camera=Camera(
        #         viewmat=torch.tensor(
        #             [
        #                 [ 0.,  0.,  1.,  8.],
        #                 [ 0.,  1.,  0.,  0.],
        #                 [-1.,  0.,  0.,  0.],
        #                 [ 0.,  0.,  0.,  1.]
        #             ]
        #         ),
        #         name="right",
        #     )
        # ),
    ]
    debug_cameras = [
        Camera(
            viewmat=torch.tensor(
                [
                    [np.cos(theta), 0., np.sin(theta), 4.],
                    [0., 1., 0., 0.],
                    [-np.sin(theta), 0., np.cos(theta), 4.],
                    [0., 0., 0., 1.]
                ]
            ),
            name="skew",
        ),
    ]
    trainer = SimpleTrainer(data=input, debug_cameras=debug_cameras)
    trainer.train()


if __name__ == "__main__":
    tyro.cli(main)
