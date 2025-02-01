import queue
import threading
import time
from typing import List

from tqdm import tqdm

from script_generator.constants import QUEUE_MAXSIZE, SEQUENTIAL_MODE, UPDATE_PROGRESS_INTERVAL
from script_generator.gui.messages.messages import ProgressMessage
from script_generator.object_detection.post_process_worker import PostProcessWorker
from script_generator.object_detection.yolo_worker import YoloWorker
from script_generator.state.app_state import AppState
from script_generator.tasks.abstract_task_processor import TaskProcessorTypes
from script_generator.tasks.tasks import AnalyzeVideoTask, AnalyzeFrameTask
from script_generator.utils.file import check_create_output_folder
from script_generator.debug.logger import log
from script_generator.video.workers.ffmpeg_worker import VideoWorker
from script_generator.video.workers.vr_to_2d_worker import VrTo2DWorker


def analyze_video(state: AppState) -> List[AnalyzeFrameTask]:
    log.info(f"[OBJECT DETECTION] Starting up pipeline{' in sequential mode' if SEQUENTIAL_MODE else ''}...")

    log_thread_stop_event = threading.Event()
    threads = []

    try:
        # make sure the output folder exists for this video
        check_create_output_folder(state.video_path)

        # Initialize batch task
        state.set_video_info()
        if state.video_reader == "FFmpeg + OpenGL (Windows)":
            if not state.video_info.is_vr:
                log.warn("Disabled OpenGL in the pipeline as it's not needed for 2D videos")
                state.video_reader = "FFmpeg"

            if state.video_info.is_fisheye:
                log.warn("Disabled OpenGL as fisheye is not yet supported with the opengl feature")
                state.video_reader = "FFmpeg"

        use_open_gl = state.video_reader == "FFmpeg + OpenGL (Windows)"

        state.analyze_task = AnalyzeVideoTask()

        # Create queues
        opengl_q = queue.Queue(maxsize=QUEUE_MAXSIZE)
        yolo_q = queue.Queue(maxsize=QUEUE_MAXSIZE)
        analysis_q = queue.Queue(maxsize=QUEUE_MAXSIZE)
        result_q = queue.Queue(maxsize=0)

        # Create threads
        decode_thread = VideoWorker(state=state, output_queue=opengl_q if use_open_gl else yolo_q)
        opengl_thread = VrTo2DWorker(state=state, input_queue=opengl_q, output_queue=yolo_q) if use_open_gl else None
        yolo_thread = YoloWorker(state=state, input_queue=yolo_q, output_queue=analysis_q)
        yolo_analysis_thread = PostProcessWorker(state=state, input_queue=analysis_q, output_queue=result_q)

        # Start logging thread
        queue_logging_thread = threading.Thread(
            target=log_progress,
            args=(state, opengl_q, yolo_q, analysis_q, result_q, log_thread_stop_event),
            daemon=True,
        )
        queue_logging_thread.start()

        # Sequential mode can be used to determine performance bottlenecks on very short videos
        if SEQUENTIAL_MODE:
            def run_thread(thread, thread_name, out_queue):
                start_time = time.time()
                thread.start()
                thread.join()
                out_queue.put(None)
                log.info(f"[OBJECT DETECTION] {thread_name} thread done in {time.time() - start_time} s")

            run_thread(decode_thread, TaskProcessorTypes.VIDEO, opengl_q)
            if use_open_gl:
                run_thread(opengl_thread, TaskProcessorTypes.OPENGL, yolo_q)
            run_thread(yolo_thread, TaskProcessorTypes.YOLO, analysis_q)
            run_thread(yolo_analysis_thread, TaskProcessorTypes.YOLO_ANALYSIS, result_q)
        else:
            threads = [decode_thread, opengl_thread, yolo_thread, yolo_analysis_thread] if use_open_gl else [decode_thread, yolo_thread, yolo_analysis_thread]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            # Check for exceptions in threads
            for thread in threads:
                if thread is not None:
                    thread.check_exception()

        state.analyze_task.end_time = time.time()

        log_thread_stop_event.set()

        log_performance(state=state, results_queue=result_q)

        if state.update_ui:
            state.update_ui(ProgressMessage(
                process="OBJECT_DETECTION",
                frames_processed=state.video_info.total_frames,
                total_frames=state.video_info.total_frames,
                eta="Done"
            ))

        return result_q.queue

    except Exception as e:
        log.error(f"An error occurred during video analysis: {e}")
        # Signal all threads to stop and perform cleanup
        log_thread_stop_event.set()
        for thread in threads:
            if thread is not None and thread.is_alive():
                thread.join(timeout=1)
        raise


def log_progress(state, opengl_q, yolo_q, analysis_q, results_q, stop_event):
    total_frames = state.video_info.total_frames

    label = 'Analyzing ' + ('VR' if state.video_info.is_vr else '2D') + ' video'

    with tqdm(
            total=total_frames,
            #desc="Analyzing video",
            desc=label,
            unit="f",
            position=0,
            unit_scale=False,
            unit_divisor=1
    ) as progress_bar:
        while not stop_event.is_set():
            opengl_size = opengl_q.qsize()
            yolo_size = yolo_q.qsize()
            analysis_size = analysis_q.qsize()
            frames_processed = results_q.qsize()

            progress_bar.n = frames_processed
            open_gl = f"OpenGL: {opengl_size:>3}, " if state.video_reader == "FFmpeg + OpenGL (Windows)" else ""
            progress_bar.set_postfix_str(
                f"Q's: {open_gl}YOLO: {yolo_size:>3}, Analysis: {analysis_size:>3}"
            )
            progress_bar.refresh()

            if frames_processed >= total_frames:
                stop_event.set()

            if state.update_ui:
                elapsed_time = time.time() - state.analyze_task.start_time
                processing_rate = frames_processed / elapsed_time if elapsed_time > 0 else 0
                remaining_frames = total_frames - frames_processed
                eta = remaining_frames / processing_rate if processing_rate > 0 else float('inf')
                try:
                    state.update_ui(ProgressMessage(
                        process="OBJECT_DETECTION",
                        frames_processed=frames_processed,
                        total_frames=total_frames,
                        eta=time.strftime("%H:%M:%S", time.gmtime(eta)) if eta != float('inf') else "Calculating..."
                    ))
                except Exception as e:
                    log.error(f"Error in state.update_ui: {e}")

            time.sleep(UPDATE_PROGRESS_INTERVAL)

def log_performance(state, results_queue):
    analyze_task = state.analyze_task
    # TODO filter out sentinals in task processor
    tasks = [task for task in results_queue.queue if hasattr(task, 'profile')]
    total_frames = len(tasks)

    total_pipeline_time = analyze_task.end_time - analyze_task.start_time
    video_duration = total_frames / state.video_info.fps
    avg_processing_fps = total_frames / total_pipeline_time
    realtime_percentage = (avg_processing_fps / 60.0) * 100.0

    log_message = (
        f"\n{'-' * 60}"
        f"\n OBJECT DETECTION COMPLETED {'(sequential mode)' if SEQUENTIAL_MODE else ''}\n"
        f"\n Settings\n"
        f"  - Video reader               : {state.video_reader}\n"     
        f"\n Video stats\n"
        f"  - Total Frames               : {total_frames}\n"
        f"  - Video Duration             : {video_duration:.2f} s\n"
    )

    if SEQUENTIAL_MODE:
        log_message += f"\n Sequential Queue statistics\n"
        for key, total_time in analyze_task.profile.items():
            if key.endswith("_duration"):  # Only include duration metrics
                avg_time = total_time / total_frames if total_frames > 0 else 0.0
                stage_name = key.replace("_duration", "").capitalize()
                log_message += (
                    f"  - {stage_name:<27}: {avg_time * 1000:.0f} ms | "
                    f"{(1 / avg_time if avg_time > 0 else 0):.0f} fps\n"
                )
    else:
        log_message += (
            f"\n Performance stats\n"
            f"  - Average Processing         : {avg_processing_fps:.2f} fps\n"
            f"  - Real-time Processing       : {realtime_percentage:.2f} %\n"
            f"  - Total Pipeline Runtime (s) : {total_pipeline_time:.2f} s\n"
        )
        log_message += f"\n Task Average Times (while running in parallel)\n"

        aggregated_times = {}
        for task in tasks:
            for key, total_time in task.profile.items():
                if key.endswith("_duration"):
                    if key not in aggregated_times:
                        aggregated_times[key] = {"total_time": 0, "task_count": 0}
                    aggregated_times[key]["total_time"] += total_time
                    aggregated_times[key]["task_count"] += 1

        # Calculate and format averages for each key
        for key, data in aggregated_times.items():
            avg_time = data["total_time"] / total_frames if total_frames > 0 else 0.0
            stage_name = key.replace("_duration", "").replace("_", " ").capitalize()
            log_message += (
                f"  - {stage_name:<27}: {avg_time * 1000:.0f} ms | "
                f"{(1 / avg_time if avg_time > 0 else 0):.0f} fps\n"
            )

    log_message += f"{'-' * 60}\n"

    for line in log_message.splitlines():
        log.info(line)