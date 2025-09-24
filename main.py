# -*- coding: utf-8 -*-
import time
from requests import Session
from requests.auth import HTTPDigestAuth
from zeep.transports import Transport
from onvif import ONVIFCamera
from lxml import etree

CAM_HOST = '192.168.1.149'
CAM_PORT = 2020
CAM_USER = 'arnaunl3'
CAM_PASS = 'rEURO841.'

PP_NS_KEY = 'http://www.onvif.org/ver10/events/wsdl/PullPointSubscription'


def make_cam():
    sess = Session()
    sess.auth = HTTPDigestAuth(CAM_USER, CAM_PASS)
    transport = Transport(session=sess, timeout=10)
    return ONVIFCamera(
        CAM_HOST, CAM_PORT, CAM_USER, CAM_PASS,
        transport=transport, adjust_time=True, encrypt=True
    )


def create_subscription(events):
    variants = [
        None,
        {'InitialTerminationTime': 'PT10M'},
        {'Filter': None, 'InitialTerminationTime': 'PT10M', 'SubscriptionPolicy': None},
    ]
    last_exc = None
    for req in variants:
        try:
            return events.CreatePullPointSubscription(req) if req else events.CreatePullPointSubscription()
        except Exception as e:
            last_exc = e
    raise last_exc


def iter_simple_items_from_xml(message_el):
    for si in message_el.xpath('.//*[local-name()="SimpleItem"]'):
        yield si.get('Name'), si.get('Value')


def loop_pull(pp):
    try:
        pp.SetSynchronizationPoint()
    except Exception:
        pass

    last_people = False
    while True:
        res = pp.PullMessages({'Timeout': 'PT5S', 'MessageLimit': 50})
        for notif in getattr(res, 'NotificationMessage', []) or []:
            msg = getattr(notif, 'Message', None)
            message_el = getattr(msg, '_value_1', None) if msg else None
            if message_el is None:
                continue

            for name, value in iter_simple_items_from_xml(message_el):
                if name == 'IsPeople':
                    v = isinstance(value, str) and value.strip().lower() in ('true', '1', 'yes', 'on')
                    if v and not last_people:
                        print('[PEOPLE] True')
                    last_people = v

        time.sleep(0.05)


def main():
    cam = make_cam()
    events = cam.create_events_service()
    sub = create_subscription(events)
    pullpoint_url = sub.SubscriptionReference.Address._value_1

    cam.xaddrs[PP_NS_KEY] = pullpoint_url
    pp = cam.create_pullpoint_service()

    try:
        addr = pp.ws_client._binding_options.get('address')
    except Exception:
        addr = pullpoint_url
    print(f"[READY] PullPoint: {addr}")

    loop_pull(pp)


if __name__ == '__main__':
    main()
