# -*- coding: utf-8 -*-
import time

from onvif import ONVIFCamera

CAM_HOST = '192.168.1.149'
CAM_PORT = 2020
CAM_USER = 'arnaunl3'
CAM_PASS = 'rEURO841.'


def main():
    """
    Main function to initialize the ONVIF camera.
    :return:
    """
    try:
        mycam = ONVIFCamera(CAM_HOST, CAM_PORT, CAM_USER, CAM_PASS)
        pullpoint = mycam.create_pullpoint_service()

        while True:
            req = pullpoint.create_type('PullMessages')
            req.Timeout = 'PT5S'
            req.MessageLimit = 100

            messages = pullpoint.PullMessages(req)

            if hasattr(messages, 'NotificationMessage'):
                for msg in messages.NotificationMessage:
                    event_data = getattr(getattr(msg, 'Message', None), 'Message', None)

                    if event_data:
                        try:
                            for item in event_data.Data.SimpleItem:
                                if item.Name == 'IsPerson' and item.Value is True:
                                    print("Person detected!")
                                    # TODO: Rest of the logic
                        except (AttributeError, TypeError):
                            pass

            time.sleep(1)

    except Exception as e:
        print(f"An error occurred: {e}")


if __name__ == '__main__':
    main()
