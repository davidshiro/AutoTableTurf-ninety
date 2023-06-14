import datetime
import io
from typing import Optional

import cv2
import numpy as np
from flask import Response, request, render_template

from capture import VideoCapture
from controller import DummyController, Controller, NxbtController
from logger import logger
from portal.debug.debugger import web_debugger
from portal.home.capture import ThreadSafeCapture
from portal.home.closer import WebCloser
from portal.home.keymap import keymap
from tableturf.ai.alpha import Alpha
from tableturf.manager import TableTurfManager, Profile


def list_available_source():
    index = 0
    arr = []
    while True:
        try:
            cap = cv2.VideoCapture(index)
            if not cap.read()[0]:
                break
            else:
                arr.append(index)
        except:
            logger.error("indexed source is unavailable")
        cap.release()
        index += 1
    return arr


available_sources = list_available_source()
capture = ThreadSafeCapture(VideoCapture(0))
controller: Optional[Controller] = None
closer: WebCloser = None


def main():
    global available_sources
    available_sources = list_available_source()
    return Response(render_template(
        'home.html',
        url=request.url,
        sources=available_sources,
        keymap=keymap
    ))


def run():
    global closer
    debug = request.json['debug']
    sleep = request.json['sleep']
    profile = request.json['profile']
    stop_at = request.json['stop_at']
    try:
        time = datetime.datetime.fromisoformat(stop_at)
    except:
        time = None
    logger.debug(f'portal.home.run: profile={profile}, sleep={sleep}, debug={debug}, stop_at={stop_at}')
    alpha_ai = Alpha()
    manager = TableTurfManager(
        capture,
        controller if controller is not None else DummyController(),
        alpha_ai,
        web_debugger,
    )
    if closer is None:
        closer = WebCloser(time)
    manager.run(profile=Profile.from_json(profile), closer=closer, debug=debug)
    closer = None
    if sleep:
        controller.press_buttons([Controller.Button.HOME], down=3)
        controller.press_buttons([Controller.Button.A])
    return Response()


def stop():
    if closer is not None:
        closer.set_close()
    return Response()


def change_source():
    global capture
    source = int(request.json['source'])
    capture.update_capture(VideoCapture(source))
    logger.debug(f'portal.home.source_on_change: source={source}')
    return Response()


def generate_frames():
    empty = np.zeros((1080, 1920, 3))
    _, empty_buf = cv2.imencode(".png", empty)
    empty_frame = bytes(io.BytesIO(empty_buf).getbuffer())
    while True:
        try:
            img = capture.capture()
            _, buffer = cv2.imencode(".png", img)
            frame = bytes(io.BytesIO(buffer).getbuffer())
            yield b'--frame\r\nContent-Type: image/png\r\n\r\n' + frame + b'\r\n'
        except Exception:
            yield b'--frame\r\nContent-Type: image/png\r\n\r\n' + empty_frame + b'\r\n'


def video_feed():
    return Response(generate_frames(), mimetype='multipart/x-mixed-replace; boundary=frame')


def key_press():
    if controller is None:
        return Response()
    event_type = request.json['type']
    if event_type == 'keydown':
        raw = request.json['key']
        key = keymap.get(raw, None)
        if key is not None:
            if key == Controller.Stick.L_STICK:
                if raw == 'a':
                    controller.tilt_stick(key, -100, 0, tilted=0.3)
                elif raw == 's':
                    controller.tilt_stick(key, 0, -100, tilted=0.3)
                elif raw == 'd':
                    controller.tilt_stick(key, 100, 0, tilted=0.3)
                elif raw == 'w':
                    controller.tilt_stick(key, 0, 100, tilted=0.3)
            else:
                controller.press_buttons([key])
        if raw == "r":
            available_sources = list_available_source()
            logger.info("refreshed sources")
    # TODO: support long press
    return Response()


def connect_controller():
    global controller
    endpoint = request.json['endpoint']
    if endpoint != '':
        controller = NxbtController(endpoint)
    else:
        controller = DummyController()
    return Response()
