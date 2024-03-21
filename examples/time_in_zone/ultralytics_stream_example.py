import argparse
import json
from datetime import datetime
from typing import Dict, Generator, List

import cv2
import numpy as np
from ultralytics import YOLO

import supervision as sv

COLORS = sv.ColorPalette.from_hex(["#E6194B", "#3CB44B", "#FFE119", "#3C76D1"])

COLOR_ANNOTATOR = sv.ColorAnnotator(color=COLORS)
LABEL_ANNOTATOR = sv.LabelAnnotator(
    color=COLORS, text_color=sv.Color.from_hex("#000000")
)


def load_zones_config(file_path: str) -> List[np.ndarray]:
    """
    Load polygon zone configurations from a JSON file.

    This function reads a JSON file which contains polygon coordinates, and
    converts them into a list of NumPy arrays. Each polygon is represented as
    a NumPy array of coordinates.

    Args:
    file_path (str): The path to the JSON configuration file.

    Returns:
    List[np.ndarray]: A list of polygons, each represented as a NumPy array.
    """
    with open(file_path, "r") as file:
        data = json.load(file)
        return [np.array(polygon, np.int32) for polygon in data]


def get_stream_frames_generator(rtsp_url: str) -> Generator[np.ndarray, None, None]:
    """
    Generator function to yield frames from an RTSP stream.

    Args:
        rtsp_url (str): URL of the RTSP video stream.

    Yields:
        np.ndarray: The next frame from the video stream.
    """
    cap = cv2.VideoCapture(rtsp_url)
    if not cap.isOpened():
        raise Exception("Error: Could not open video stream.")

    try:
        while True:
            ret, frame = cap.read()
            if not ret:
                print("End of stream or error reading frame.")
                break
            yield frame
    finally:
        cap.release()


class ClockTimeBasedTimer:
    """
    A class for tracking and updating time durations associated with object detections.

    Attributes:
        tracker_id2start_time (Dict[int, datetime]): A dictionary mapping tracker IDs
            to the datetime when they were first detected.
    """

    def __init__(self) -> None:
        self.tracker_id2start_time: Dict[int, datetime] = {}

    def tick(self, detections: sv.Detections) -> np.ndarray:
        """
        Updates detection times based on current detections.

        Args:
            detections (sv.Detections): The current detections with tracker IDs.

        Returns:
            np.ndarray: The time duration each object has been detected.
        """
        current_time = datetime.now()
        times = []

        for tracker_id in detections.tracker_id:
            self.tracker_id2start_time.setdefault(tracker_id, current_time)

            start_time = self.tracker_id2start_time[tracker_id]
            time_duration = (current_time - start_time).total_seconds()
            times.append(time_duration)

        return np.array(times)


def main(
    rtsp_url: str,
    zone_configuration_path: str,
    weights: str,
    device: str,
    confidence: float,
    iou: float,
) -> None:
    model = YOLO(weights)
    tracker = sv.ByteTrack(minimum_matching_threshold=0.5)
    frames_generator = get_stream_frames_generator(rtsp_url=rtsp_url)
    fps_monitor = sv.FPSMonitor()

    frame = next(frames_generator)
    resolution_wh = frame.shape[1], frame.shape[0]

    polygons = load_zones_config(file_path=zone_configuration_path)
    zones = [
        sv.PolygonZone(
            polygon=polygon,
            frame_resolution_wh=resolution_wh,
            triggering_anchors=(sv.Position.CENTER,),
        )
        for polygon in polygons
    ]
    timers = [ClockTimeBasedTimer() for _ in zones]

    for frame in frames_generator:
        fps_monitor.tick()
        fps = fps_monitor.fps

        results = model(frame, verbose=False, device=device, conf=confidence)[0]
        detections = sv.Detections.from_ultralytics(results)
        detections = detections[detections.class_id == 0]
        detections = detections.with_nms(threshold=iou)
        detections = tracker.update_with_detections(detections)

        annotated_frame = frame.copy()
        annotated_frame = sv.draw_text(
            scene=annotated_frame,
            text=f"{fps:.1f}",
            text_anchor=sv.Point(40, 30),
            background_color=sv.Color.from_hex("#A351FB"),
            text_color=sv.Color.from_hex("#000000"),
        )

        for idx, zone in enumerate(zones):
            annotated_frame = sv.draw_polygon(
                scene=annotated_frame, polygon=zone.polygon, color=COLORS.by_idx(idx)
            )

            detections_in_zone = detections[zone.trigger(detections)]
            time_in_zone = timers[idx].tick(detections_in_zone)
            custom_color_lookup = np.full(detections_in_zone.class_id.shape, idx)

            annotated_frame = COLOR_ANNOTATOR.annotate(
                scene=annotated_frame,
                detections=detections_in_zone,
                custom_color_lookup=custom_color_lookup,
            )
            labels = [
                f"#{tracker_id} {int(time // 60):02d}:{int(time % 60):02d}"
                for tracker_id, time in zip(detections_in_zone.tracker_id, time_in_zone)
            ]
            annotated_frame = LABEL_ANNOTATOR.annotate(
                scene=annotated_frame,
                detections=detections_in_zone,
                labels=labels,
                custom_color_lookup=custom_color_lookup,
            )

        cv2.imshow("Processed Video", annotated_frame)
        if cv2.waitKey(1) & 0xFF == ord("q"):
            break
    cv2.destroyAllWindows()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Calculating detections dwell time in zones, using RTSP stream."
    )
    parser.add_argument(
        "--zone_configuration_path",
        type=str,
        required=True,
        help="Path to the zone configuration JSON file.",
    )
    parser.add_argument(
        "--rtsp_url",
        type=str,
        required=True,
        help="Complete RTSP URL for the video stream.",
    )
    parser.add_argument(
        "--weights",
        type=str,
        default="yolov8s.pt",
        help="Path to the model weights file. Default is 'yolov8s.pt'.",
    )
    parser.add_argument(
        "--device",
        type=str,
        default="cpu",
        help="Computation device ('cpu', 'mps' or 'cuda'). Default is 'cpu'.",
    )
    parser.add_argument(
        "--confidence_threshold",
        type=float,
        default=0.3,
        help="Confidence level for detections (0 to 1). Default is 0.3.",
    )
    parser.add_argument(
        "--iou_threshold",
        default=0.7,
        type=float,
        help="IOU threshold for non-max suppression. Default is 0.7.",
    )
    args = parser.parse_args()

    main(
        rtsp_url=args.rtsp_url,
        zone_configuration_path=args.zone_configuration_path,
        weights=args.weights,
        device=args.device,
        confidence=args.confidence_threshold,
        iou=args.iou_threshold,
    )