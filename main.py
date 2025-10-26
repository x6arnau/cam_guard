# -*- coding: utf-8 -*-
import os
import sys
import time

import logging
from dotenv import load_dotenv
from lxml import etree
from onvif import ONVIFCamera
from requests import Session
from requests.auth import HTTPDigestAuth
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
        logger.info("[STOP]")
        pass
    except Exception as e:
        logger.error(f"[FATAL] {e}")
        sys.exit(1)
