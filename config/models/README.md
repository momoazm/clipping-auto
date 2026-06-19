# Face-detection model (YuNet)

`reframe_crop.py` uses OpenCV's **YuNet** DNN face detector for the 9:16
speaker-tracking reframe — it catches profile / angled / smaller faces that the
Haar cascade misses (so the crop locks onto the speaker from the first frame).
If this model file is missing, the tool automatically falls back to the bundled
Haar cascade.

Download (once, ~230 KB), save into this folder as
`face_detection_yunet_2023mar.onnx`:

```
https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx
```

The `.onnx` binary is gitignored (downloaded, not committed). No extra pip
dependency is needed — `cv2.FaceDetectorYN` ships with `opencv-python`.
