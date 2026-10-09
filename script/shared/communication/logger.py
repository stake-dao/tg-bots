import json


import logging


import os


from datetime import datetime


from dotenv import load_dotenv


from shared.utils.globals import is_production


load_dotenv()


class TelegramLogger:
    def __init__(self, keep_memory=False, force_log=False):
        self.log_file_path = "json/logs.json"
        self.force_log = force_log

        # Overwrite the log file if it exists upon logger creation
        if not keep_memory:
            if os.path.exists(self.log_file_path):
                with open(self.log_file_path, "w") as log_file:
                    log_file.write("[]")  # Initialize with an empty list
        else:
            # Do not overwrite and continue to write in it
            if os.path.exists(self.log_file_path):
                with open(self.log_file_path, "a") as log_file:
                    log_file.write("[]")  # Initialize with an empty list

    def log(self, message):
        timestamp = datetime.now().strftime("%d/%m : %H:%M")
        log_entry = {timestamp: message}

        if is_production() or self.force_log == True:
            try:
                # Read the existing log file into a list
                with open(self.log_file_path, "r") as log_file:
                    logs = json.load(log_file)
            except (FileNotFoundError, json.JSONDecodeError):
                logs = []

            # Append the new log entry to the list of logs
            logs.append(log_entry)

            # Write the updated list of logs back to the file
            with open(self.log_file_path, "w") as log_file:
                json.dump(logs, log_file, indent=4)

        clean_message = (
            message.replace("<b>", "")
            .replace("</b>", "")
            .replace("<i>", "")
            .replace("</i>", "")
        )
        logging.info(clean_message)


logger = TelegramLogger(keep_memory=False)
