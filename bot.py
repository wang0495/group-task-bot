import nonebot
from nonebot.adapters.qq import Adapter as QQAdapter

nonebot.init()
driver = nonebot.get_driver()
driver.register_adapter(QQAdapter)

nonebot.load_plugins("src/plugins")

from src.plugins.task_manager.models import init_db  # noqa: E402
driver.on_startup(init_db)

if __name__ == "__main__":
    nonebot.run()
