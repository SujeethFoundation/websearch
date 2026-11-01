import os, sys
from azure.storage.queue import QueueClient

conn = os.environ["QCONN"]
for name in sys.argv[1:]:
    props = QueueClient.from_connection_string(conn, name).get_queue_properties()
    print(props.approximate_message_count)