import cv2
import glob
import numpy as np
from typing import Generator, Literal
from matplotlib.patches import Circle, Rectangle
import matplotlib.pyplot as plt
import torch

import sys
sys.path.append('./../')

from utils.libs.BlazeFace.blazeface import BlazeFace

def take_photo_from_camera(camera: cv2.VideoCapture, patience=3) -> Generator[np.ndarray, None, None]:
  fail_count = 0
  while True:
    if not camera.isOpened():
      raise Exception("Could not open video device")

    ok, frame = camera.read()
    if fail_count > patience:
      raise EOFError("Camera failed to give frames.")
    if not ok:
      fail_count += 1
      continue
    fail_count = 0
    yield frame

def take_photo() -> np.ndarray: 
  camera = cv2.VideoCapture(0)
  frame = next(take_photo_from_camera(camera))
  camera.release()
  return frame

def reverse_channels(img: np.ndarray) -> np.ndarray:
  return img[:, :, ::-1] # equivalent to cv2.cvtColor(img, cv2.COLOR_BGR2RGB)

def downsize_img(img: np.ndarray, target_size: tuple[int, int]):
  return cv2.resize(img, target_size)

def get_torch_device():
  print("PyTorch version:", torch.__version__)
  print("CUDA version:", torch.version.cuda)
  print("cuDNN version:", torch.backends.cudnn.version())
  device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
  return device

def load_blazeface(path:str, device: torch.device) -> BlazeFace:
  model = BlazeFace().to(device)
  model.load_weights(path + "blazeface.pth")
  model.load_anchors(path + "anchors.npy")
  return model

def configure_params(model, min_score_thresh, min_suppression_threshold):
  model.min_score_thresh = min_score_thresh
  model.min_suppression_threshold = min_suppression_threshold

def plot_detections(img, detections, with_keypoints=True, title="Detections"):
    _, ax = plt.subplots(1, figsize=(5, 5))
    ax.set_title(title)
    ax.grid(False)
    ax.imshow(img)
    
    if isinstance(detections, torch.Tensor):
        detections = detections.cpu().numpy()

    if detections.ndim == 1:
        detections = np.expand_dims(detections, axis=0)

    print("Found %d faces" % detections.shape[0])
        
    for i in range(detections.shape[0]):
        y_min = detections[i, 0] * img.shape[0]
        x_min = detections[i, 1] * img.shape[1]
        y_max = detections[i, 2] * img.shape[0]
        x_max = detections[i, 3] * img.shape[1]

        rect = Rectangle((x_min, y_min), x_max - x_min, y_max - y_min,
                                 linewidth=1, edgecolor="g", facecolor="none", 
                                 alpha=detections[i, 16])
        ax.add_patch(rect)

        CIRCLE_SETTINGS = {"radius": 0.5, "linewidth": 2, "edgecolor": "r", "facecolor": "none"}
        if with_keypoints:
            for k in range(6):
                kp_x = detections[i, 4 + k*2    ] * img.shape[1]
                kp_y = detections[i, 4 + k*2 + 1] * img.shape[0]
                circle = Circle((kp_x, kp_y), **CIRCLE_SETTINGS, alpha=detections[i, 16])
                ax.add_patch(circle)
    plt.show()


def get_idx_of_biggest_face(detections: np.ndarray) -> int: 
   return int(np.apply_along_axis(lambda x: (x[2] - x[0]) * (x[3] - x[1]), 1, detections).argmax())

def reset_face_angle(img: np.ndarray, detections: list) -> np.ndarray:
  right_eye = (int(detections[4] * img.shape[1]), int(detections[5] * img.shape[0]))
  left_eye = (int(detections[6] * img.shape[1]), int(detections[7] * img.shape[0]))

  w, h = img.shape[:2]

  angle = np.arctan2(left_eye[1] - right_eye[1], left_eye[0] - right_eye[0])
  angle = np.degrees(angle)

  M = cv2.getRotationMatrix2D(right_eye, angle, scale=1)
  rotated = cv2.warpAffine(img, M, (w, h))

  return rotated

def maintain_ratio(y_min, x_min, y_max, x_max, ratio: tuple[int, int]) -> tuple[int, int, int, int]:
  h = y_max - y_min
  w = x_max - x_min

  if h * ratio[0] > w * ratio[1]:
    pad_w = int(h / ratio[1] - w / ratio[0]) 
    pad_h_left = pad_w // 2
    x_min -= pad_h_left
    x_max += pad_w - pad_h_left

  elif h * ratio[0] < w * ratio[1]:
    pad_h = int(w / ratio[0] - h / ratio[1])
    pad_w_top = pad_h // 2
    y_min -= pad_w_top
    y_max += pad_h - pad_w_top

  return y_min, x_min, y_max, x_max

class Margin:
  def __init__(self, top: int|str, right: int|str, left: int|str, bottom: int|str):
      self.top = top
      self.right = right
      self.left = left
      self.bottom = bottom

  def get_margin(self, value: int, margin: int|str):
    if isinstance(margin, int):
      return margin
    elif margin.endswith("%"):
      return int(value * int(margin[:-1]) / 100)
  
  def _put_coordinates_in_range(self, value: int, max_value: int):
    return max(0, min(value, max_value))

  def recalculate_coordinates(self, y_min, x_min, y_max, x_max, img_height, img_width, ratio: tuple[int, int]):
    y_min -= self.get_margin(y_min, self.top)
    x_min -= self.get_margin(x_min, self.left)
    y_max += self.get_margin(y_max, self.bottom)
    x_max += self.get_margin(x_max, self.right)

    y_min, x_min, y_max, x_max = maintain_ratio(y_min, x_min, y_max, x_max, ratio) 

    y_min = self._put_coordinates_in_range(y_min, img_height)
    x_min = self._put_coordinates_in_range(x_min, img_width)
    y_max = self._put_coordinates_in_range(y_max, img_height)
    x_max = self._put_coordinates_in_range(x_max, img_width)
    return y_min, x_min, y_max, x_max  


def crop_face_from_img(img: np.ndarray, detections: np.ndarray, margin: Margin, ratio: tuple[int, int]) -> np.ndarray:
  y_min = int(detections[0] * img.shape[0])
  x_min = int(detections[1] * img.shape[1])
  y_max = int(detections[2] * img.shape[0])
  x_max = int(detections[3] * img.shape[1])

  y_min, x_min, y_max, x_max = margin.recalculate_coordinates(y_min, x_min, y_max, x_max, img.shape[0], img.shape[1], ratio)
  return img[y_min:y_max, x_min:x_max]

BLAZEFACE_INPUT_SIZE = (128, 128)

def create_padding(height: int, width: int, channels, dtype=np.uint8) -> np.ndarray:
  return np.zeros((height, width, channels), dtype=dtype)

def make_image_rectangle(img: np.ndarray) -> np.ndarray:
  h, w, c = img.shape
  if h > w:
    pad_w = h - w
    pad_left_w = pad_w // 2
    pad_right_w = pad_w - pad_left_w

    padding_left = create_padding(h, pad_left_w, c)
    padding_right = create_padding(h, pad_right_w, c)
    return np.hstack([padding_left, img, padding_right])

  elif w > h: 
    pad_h = w - h
    pad_top_h = pad_h // 2
    pad_bot_h = pad_h - pad_top_h

    padding_top = create_padding(pad_top_h, w, c)
    padding_bot = create_padding(pad_bot_h, w, c)
    return np.vstack([padding_top, img, padding_bot])
  return img

class PhaseOne:
  def __init__(self, margin: Margin = Margin("30%", 30, 30, 5)):
    self.device = get_torch_device()
    self.margin = margin 
    self.model = load_blazeface("../utils/libs/BlazeFace/", self.device)
    configure_params(self.model, 0.75, 0.3)

  def _predict(self, img: np.ndarray):
    downsized_img = downsize_img(img, BLAZEFACE_INPUT_SIZE)
    detections = self.model.predict_on_image(downsized_img)
    return detections
  
  def run(self, img: np.ndarray) -> tuple[str, np.ndarray]:
    padded_img = make_image_rectangle(img)

    downsized = downsize_img(padded_img, BLAZEFACE_INPUT_SIZE)
    detections = self._predict(downsized)

    if len(detections) == 0:
      return "Warning: No face detected. Returning original image", img
    
    biggest_face_idx = get_idx_of_biggest_face(detections)
    rotated = reset_face_angle(padded_img, detections[biggest_face_idx])

    downsized = downsize_img(rotated, BLAZEFACE_INPUT_SIZE) 
    detections = self._predict(downsized)
    if len(detections) == 0:
      return "Warning: Couldn't find face after resetting the angle. Returning original image", img
    
    biggest_face_idx = get_idx_of_biggest_face(detections)
    cropped_image = crop_face_from_img(rotated, detections[biggest_face_idx], self.margin, (1,1))
    return "", cropped_image

def handle_window():
  key = cv2.waitKey(0)
  cv2.destroyWindow("Face")

  if key == ord('q'):
    return 'exit'
  return 'continue'

def handle_action(action_name: Literal['show', 'save'], img: np.ndarray):
  if action_name == 'show':
    cv2.imshow("Face", cv2.cvtColor(out, cv2.COLOR_BGR2RGB))
    if handle_window() == 'exit': exit()
  else:
    cv2.imwrite(img_path, cv2.cvtColor(out, cv2.COLOR_BGR2RGB)) 

if __name__ == "__main__": 
   # dont run it from project root. Enter any folder like /utils/ or /src/ 
  phaseOne = PhaseOne()

  img_paths = glob.glob('../database/kuba.jpeg')
  print("found", len(img_paths), "images")

  for i, img_path in enumerate(img_paths):
    img = cv2.imread(img_path)
    img = reverse_channels(img)
    msg, out = phaseOne.run(img)

    if msg:
      print(f"{i}: {img_path} {msg}")
    handle_action("save", out)
