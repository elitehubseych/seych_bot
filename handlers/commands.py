from handlers.registry import command

COMMANDS_LINK = (
    "📋 Полный список команд доступен по ссылке:\n"
    "https://vk.cc/d0PMJW"
)


@command("команды", "команда")
def cmd_commands(user, args, message):
    if (args or "").strip():
        return None
    return COMMANDS_LINK
