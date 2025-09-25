# -*- coding: utf-8 -*-
import os
import signal
import sys
import time
from typing import Optional

import logging
from dotenv import load_dotenv
from lxml import etree
from onvif import ONVIFCamera
from requests import Session
from requests.auth import HTTPDigestAuth
from rtde_control import RTDEControlInterface as RTDEControl
from zeep.transports import Transport

load_dotenv()

logger = logging.getLogger("cam_guard")
if not logging.getLogger().handlers:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )

CAM_HOST = os.getenv('CAM_HOST')
CAM_PORT = int(os.getenv('CAM_PORT'))
CAM_USER = os.getenv('CAM_USER')
CAM_PASS = os.getenv('CAM_PASS')

PP_NS_KEY = 'http://www.onvif.org/ver10/events/wsdl/PullPointSubscription'

NS = {'tt': 'http://www.onvif.org/ver10/schema'}
X_ISPEOPLE_VAL = etree.XPath(
    'string(tt:Data/tt:SimpleItem[@Name="IsPeople"]/@Value)',
    namespaces=NS
)

PULL_TIMEOUT = os.getenv('PULL_TIMEOUT')
PULL_MESSAGE_LIMIT = int(os.getenv('PULL_MESSAGE_LIMIT'))
SLEEP_SEC = float(os.getenv('SLEEP_SEC'))
SCAN_PORTS = range(int(os.getenv('SCAN_PORTS_START')), int(os.getenv('SCAN_PORTS_END')))

UR_HOST = os.getenv('UR_HOST')

rtde_c: Optional[RTDEControl] = None
_rtde_last_attempt_ts = 0.0
_rtde_backoff_s = 30.0
_rtde_attempts = 0


def _graceful_exit(signum, frame):
    try:
        c = rtde_c
        if c is not None:
            c.disconnect()
    except Exception:
        pass
    logger.info("[STOP]")
    sys.exit(0)


signal.signal(signal.SIGINT, _graceful_exit)
signal.signal(signal.SIGTERM, _graceful_exit)


def make_cam():
    sess = Session()
    sess.auth = HTTPDigestAuth(CAM_USER, CAM_PASS)
    transport = Transport(session=sess, timeout=15)
    return ONVIFCamera(
        CAM_HOST, CAM_PORT, CAM_USER, CAM_PASS,
        transport=transport, adjust_time=True, encrypt=True
    )


def connect_camera_with_backoff():
    attempt = 1
    while True:
        try:
            logger.info(f"[CAM] Connecting to {CAM_HOST}:{CAM_PORT} (attempt #{attempt})...")
            cam = make_cam()
            logger.info("[CAM] Connected.")
            return cam
        except Exception as e:
            logger.error(f"[CAM] Connection error: {e}")
            logger.info("[CAM] Retrying in 30s...")
            attempt += 1
            time.sleep(30.0)


def try_create_subscription(events):
    variants = [
        None,
        {'InitialTerminationTime': 'PT60S'},
        {'InitialTerminationTime': 'PT10M'},
        {
            'Filter': {
                'TopicExpression': {
                    '_value_1': 'tns1:RuleEngine//.',
                    'Dialect': 'http://www.onvif.org/ver10/tev/topicExpression/ConcreteSet'
                }
            },
            'InitialTerminationTime': 'PT10M'
        },
        {'Filter': None, 'SubscriptionPolicy': None, 'InitialTerminationTime': 'PT10M'},
    ]
    last_exc = None
    for req in variants:
        try:
            sub = events.CreatePullPointSubscription(req) if req else events.CreatePullPointSubscription()
            return sub.SubscriptionReference.Address._value_1
        except Exception as e:
            last_exc = e
            time.sleep(0.2)
    if last_exc:
        raise last_exc


def try_probe_existing_pullpoint(cam):
    for p in SCAN_PORTS:
        url = f"http://{CAM_HOST}:{p}/event-{p}_{p}"
        try:
            cam.xaddrs[PP_NS_KEY] = url
            pp = cam.create_pullpoint_service()
            pp.PullMessages({'Timeout': 'PT1S', 'MessageLimit': 1})
            return url
        except Exception:
            continue
    return None


def ensure_rtde_connected():
    global rtde_c, _rtde_last_attempt_ts, _rtde_backoff_s, _rtde_attempts
    now = time.time()
    if now - _rtde_last_attempt_ts < _rtde_backoff_s:
        return
    _rtde_last_attempt_ts = now

    try:
        if rtde_c is None:
            _rtde_attempts += 1
            logger.info(f"[RTDE] Connecting to {UR_HOST} (attempt #{_rtde_attempts})...")
            rtde_c = RTDEControl(UR_HOST)
            logger.info("[RTDE] Connected.")
            _rtde_attempts = 0
            return

        c = rtde_c
        if c is not None and c.isConnected():
            _rtde_attempts = 0
            return

        _rtde_attempts += 1
        logger.info(f"[RTDE] Retrying connection (attempt #{_rtde_attempts})...")
        if c is not None and c.reconnect():
            logger.info("[RTDE] Reconnected.")
            _rtde_attempts = 0
            return

        try:
            if c is not None:
                c.disconnect()
        except Exception:
            pass
        logger.info(f"[RTDE] Creating new session with {UR_HOST} (attempt #{_rtde_attempts})...")
        rtde_c = RTDEControl(UR_HOST)
        logger.info("[RTDE] Connected.")
        _rtde_attempts = 0
    except Exception as e:
        logger.error(f"[RTDE] Connection error: {e}")
        logger.info("[RTDE] Will retry in 30s...")


def is_program_running():
    try:
        ensure_rtde_connected()
        c = rtde_c
        if c is None:
            return None
        status = c.getRobotStatus()
        return bool(status & 0x2)
        # return ((status >> 1) & 1) == 1
    except Exception as e:
        logger.error(f"[RTDE] getRobotStatus error: {e}")
        return None


def stop_robot():
    try:
        ensure_rtde_connected()
        c = rtde_c
        if c is None:
            return
        c.stopScript()
        logger.info("[RTDE] stopScript sent")
    except Exception as e:
        logger.error(f"[RTDE] stopScript error: {e}")


def loop_pull(pp):
    try:
        pp.SetSynchronizationPoint()
    except Exception:
        pass

    last_people = False
    while True:
        try:
            res = pp.PullMessages({'Timeout': PULL_TIMEOUT, 'MessageLimit': PULL_MESSAGE_LIMIT})
        except KeyboardInterrupt:
            logger.info("[STOP]")
            return
        for notif in getattr(res, 'NotificationMessage', []) or []:
            msg = getattr(notif, 'Message', None)
            message_el = getattr(msg, '_value_1', None) if msg else None
            if message_el is None:
                continue

            val = X_ISPEOPLE_VAL(message_el)
            v_true = (val == 'true')
            if v_true and not last_people:
                logger.info('[PEOPLE] True')
                running = is_program_running()
                if running:
                    stop_robot()
            last_people = v_true

        time.sleep(SLEEP_SEC)


def main():
    cam = connect_camera_with_backoff()
    events = cam.create_events_service()

    pullpoint_url = cam.xaddrs.get(PP_NS_KEY)

    if not pullpoint_url:
        try:
            pullpoint_url = try_create_subscription(events)
        except Exception:
            pullpoint_url = try_probe_existing_pullpoint(cam)
            if not pullpoint_url:
                raise

        cam.xaddrs[PP_NS_KEY] = pullpoint_url

    pp = cam.create_pullpoint_service()
    addr = getattr(pp.ws_client, '_binding_options', {}).get('address', pullpoint_url)
    logger.info(f"[READY] PullPoint: {addr}")

    loop_pull(pp)


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        try:
            c = rtde_c
            if c is not None:
                c.disconnect()
        except Exception:
            pass
        logger.info("[STOP]")
