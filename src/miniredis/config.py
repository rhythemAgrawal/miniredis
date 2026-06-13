class Config:
    def __init__(self, snapshot_path: str, max_save_timeout):
        self.snapshot_path = snapshot_path
        self.max_save_timeout = max_save_timeout

config = Config("dump.rdb", 3600)
