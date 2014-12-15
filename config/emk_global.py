import os

user_config = os.path.expanduser("~/.emk")
emk.import_from([user_config], "config")
