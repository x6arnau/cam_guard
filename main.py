# -*- coding: utf-8 -*-
import os
import signal
import sys
import time

from dotenv import load_dotenv
from onvif import ONVIFCamera
from requests import Session
from requests.auth import HTTPDigestAuth
from rtde_control import RTDEControlInterface as RTDEControl
from zeep.transports import Transport

load_dotenv()

CAM_HOST = os.getenv('CAM_HOST')
CAM_PORT = int(os.getenv('CAM_PORT'))
CAM_USER = os.getenv('CAM_USER')
CAM_PASS = os.getenv('CAM_PASS')

PP_NS_KEY = 'http://www.onvif.org/ver10/events/wsdl/PullPointSubscription'

PULL_TIMEOUT = os.getenv('PULL_TIMEOUT')
PULL_MESSAGE_LIMIT = int(os.getenv('PULL_MESSAGE_LIMIT'))
SLEEP_SEC = float(os.getenv('SLEEP_SEC'))
SCAN_PORTS = range(int(os.getenv('SCAN_PORTS_START')), int(os.getenv('SCAN_PORTS_END')))

UR_HOST = os.getenv('UR_HOST')

rtde_c = RTDEControl(UR_HOST)

_rtde_last_attempt_ts = 0.0
_rtde_backoff_s = 30.0


def _graceful_exit(signum, frame):
    try:
        rtde_c.disconnect()
    except Exception:
        pass
    print("[STOP]")
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
    global rtde_c, _rtde_last_attempt_ts
    now = time.time()
    if now - _rtde_last_attempt_ts < _rtde_backoff_s:
        return
    _rtde_last_attempt_ts = now

    try:
        if rtde_c.isConnected() or rtde_c.reconnect():
            return
    except Exception:
        pass

    try:
        try:
            rtde_c.disconnect()
        except Exception:
            pass
        rtde_c = RTDEControl(UR_HOST)
    except Exception:
        pass


def is_program_running():
    try:
        ensure_rtde_connected()
        status = rtde_c.getRobotStatus()
        return bool(status & 0x2)
        # return ((status >> 1) & 1) == 1
    except Exception as e:
        print(f"[RTDE] getRobotStatus error: {e}")
        return None


def stop_robot():
    try:
        ensure_rtde_connected()
        rtde_c.stopScript()
        print("[RTDE] stopScript sent")
    except Exception as e:
        print(f"[RTDE] stopScript error: {e}")


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
            print("[STOP]")
            return
        for notif in getattr(res, 'NotificationMessage', []) or []:
            msg = getattr(notif, 'Message', None)
            message_el = getattr(msg, '_value_1', None) if msg else None
            if message_el is None:
                continue

            for si in message_el.xpath('.//*[local-name()="SimpleItem"][@Name="IsPeople"]'):
                val = (si.get('Value') or '').strip().lower()
                v_true = val in ('true', '1', 'yes', 'on')
                if v_true and not last_people:
                    print('[PEOPLE] True')

                    running = is_program_running()
                    if running:
                        stop_robot()

                last_people = v_true

        time.sleep(SLEEP_SEC)


def main():
    cam = make_cam()
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
    print(f"[READY] PullPoint: {addr}")

    loop_pull(pp)


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        try:
            rtde_c.disconnect()
        except Exception:
            pass
        print("[STOP]")
