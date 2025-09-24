# main.py
# -*- coding: utf-8 -*-
import time
from requests import Session
from requests.auth import HTTPDigestAuth
from zeep.transports import Transport
from onvif import ONVIFCamera
from lxml import etree

CAM_HOST = '192.168.1.149'
CAM_PORT = 2020
CAM_USER = 'arnaunl3'    # <-- cambia
CAM_PASS = 'rEURO841.'   # <-- cambia

PP_NS_KEY = 'http://www.onvif.org/ver10/events/wsdl/PullPointSubscription'
DEBUG_PRINT = False  # pon True si quieres ver todos los SimpleItem que llegan


def as_bool(v):
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v != 0
    if isinstance(v, str):
        return v.strip().lower() in ('true', '1', 'yes', 'on')
    return bool(v)


def make_cam(encrypt=True):
    # Muchas Tapo requieren HTTP Digest en el endpoint de eventos
    sess = Session()
    sess.auth = HTTPDigestAuth(CAM_USER, CAM_PASS)
    transport = Transport(session=sess, timeout=10)
    return ONVIFCamera(
        CAM_HOST, CAM_PORT, CAM_USER, CAM_PASS,
        transport=transport, adjust_time=True, encrypt=encrypt
    )


def create_subscription(events):
    # Usa la forma más simple primero; tu cámara ya la aceptó en el debug
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
    """
    message_el es un lxml.etree._Element (<tt:Message>).
    Buscamos cualquier tt:SimpleItem en el árbol (Data/Source/Key, etc).
    """
    for si in message_el.xpath('.//*[local-name()="SimpleItem"]'):
        yield si.get('Name'), si.get('Value')


def loop_pull(pp):
    # Algunas cámaras necesitan esto para emitir el estado inicial
    try:
        pp.SetSynchronizationPoint()
    except Exception:
        pass

    last_people = False
    while True:
        res = pp.PullMessages({'Timeout': 'PT5S', 'MessageLimit': 50})
        for notif in getattr(res, 'NotificationMessage', []) or []:
            # El payload real viene en Message._value_1 como XML (<tt:Message>)
            msg = getattr(notif, 'Message', None)
            message_el = getattr(msg, '_value_1', None) if msg else None
            if message_el is None:
                continue

            for name, value in iter_simple_items_from_xml(message_el):
                if DEBUG_PRINT:
                    print(f"Item: {name} = {value}")

                if name in ('IsPeople', 'IsPerson'):
                    v = as_bool(value)
                    if v and not last_people:
                        print('hello world')
                    last_people = v

        time.sleep(0.05)


def main():
    # Probamos WSSE con digest y, si hiciera falta, WSSE en claro
    for encrypt in (True, False):
        try:
            cam = make_cam(encrypt=encrypt)
            events = cam.create_events_service()
            sub = create_subscription(events)

            pullpoint_url = sub.SubscriptionReference.Address._value_1
            print(f"[OK] PullPoint URL: {pullpoint_url} (encrypt={encrypt})")

            # Registrar xaddr del PullPoint para que el helper apunte bien
            cam.xaddrs[PP_NS_KEY] = pullpoint_url
            pp = cam.create_pullpoint_service()

            # (debug) confirma a qué URL se conecta
            try:
                print("[DBG] pullpoint addr =", pp.ws_client._binding_options.get('address'))
            except Exception:
                pass

            print("[INFO] Esperando IsPeople/IsPerson=True...")
            loop_pull(pp)
            break
        except Exception as e:
            print(f"[encrypt={encrypt}] Falló: {e}")
            continue


if __name__ == '__main__':
    main()
