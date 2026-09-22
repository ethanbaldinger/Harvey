"""Mount at /barbie in the existing PA DispatcherMiddleware."""
import json
from pathlib import Path

from .mysql_mailbox import MySQLMailbox


def create_app(config_path):
    import mysql.connector
    config = json.loads(Path(config_path).read_text(encoding='utf-8'))
    return MySQLMailbox(lambda: mysql.connector.connect(**config['mysql'], autocommit=False),
                        config['worker_token'], config['manager_token'])
