import json


import os


from datetime import datetime


from typing import Optional, Union


from dotenv import load_dotenv


from rich.console import Console


from rich.panel import Panel


from rich.style import Style


from shared.utils.globals import is_production


load_dotenv()


class RichTelegramLogger:
    def __init__(self, keep_memory: bool = False):
        self.log_file_path = "json/logs.json"
        self.console = Console()

        # Initialize log file if needed
        if not os.path.exists(os.path.dirname(self.log_file_path)):
            os.makedirs(os.path.dirname(self.log_file_path))

        if not keep_memory and os.path.exists(self.log_file_path):
            with open(self.log_file_path, "w") as log_file:
                json.dump([], log_file)
        elif not os.path.exists(self.log_file_path):
            with open(self.log_file_path, "w") as log_file:
                json.dump([], log_file)

    def _format_message(self, message: Union[str, Panel]) -> str:
        """Format message for Telegram while preserving Rich formatting"""
        if isinstance(message, Panel):
            # Convert Panel to Telegram-style formatting
            return f"<b>== {message.title or ''} ==</b>\n{message.renderable}"
        return message

    def _clean_message(self, message: Union[str, Panel]) -> str:
        """Remove formatting tags for plain text output"""
        if isinstance(message, Panel):
            return f"== {message.title or ''} ==\n{message.renderable}"

        return (
            message.replace("<b>", "")
            .replace("</b>", "")
            .replace("<i>", "")
            .replace("</i>", "")
        )

    def log(
        self,
        message: Union[str, Panel],
        style: Optional[Union[str, Style]] = None,
        emoji: Optional[str] = None,
    ):
        """
        Log a message with Rich formatting locally and to Telegram in production

        Args:
            message: String or Panel to log
            style: Rich style string or Style object
            emoji: Optional emoji prefix
        """
        timestamp = datetime.now().strftime("%d/%m : %H:%M")

        # Handle local Rich logging
        if isinstance(message, Panel):
            self.console.print(message)
        else:
            formatted_msg = []
            if emoji:
                formatted_msg.append(emoji)
            formatted_msg.append(message)

            msg_text = " ".join(formatted_msg)
            if style:
                self.console.print(msg_text, style=style)
            else:
                self.console.print(msg_text)

        # Handle production Telegram logging
        if is_production():
            try:
                with open(self.log_file_path, "r") as log_file:
                    logs = json.load(log_file)
            except (FileNotFoundError, json.JSONDecodeError):
                logs = []

            telegram_msg = self._format_message(message)
            if emoji:
                telegram_msg = f"{emoji} {telegram_msg}"

            log_entry = {timestamp: telegram_msg}
            logs.append(log_entry)

            with open(self.log_file_path, "w") as log_file:
                json.dump(logs, log_file, indent=4)

    def success(self, message: Union[str, Panel]):
        """Log a success message in green"""
        self.log(message, style="green", emoji="✅")

    def error(self, message: Union[str, Panel]):
        """Log an error message in red"""
        self.log(message, style="red bold", emoji="❌")

    def warning(self, message: Union[str, Panel]):
        """Log a warning message in yellow"""
        self.log(message, style="yellow", emoji="⚠️")

    def info(self, message: Union[str, Panel]):
        """Log an info message in blue"""
        self.log(message, style="blue", emoji="ℹ️")

    def status(self, message: Union[str, Panel]):
        """Log a status message with a special style"""
        self.log(message, style="cyan bold", emoji="🔄")


logger = RichTelegramLogger(keep_memory=False)
