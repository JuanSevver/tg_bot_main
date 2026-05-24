from aiogram.fsm.state import State, StatesGroup


class AdminSG(StatesGroup):
    dashboard = State()


class UserManageSG(StatesGroup):
    list = State()
    search = State()
    detail = State()
    grant_subscription = State()
    send_message = State()


class BroadcastSG(StatesGroup):
    choose_target = State()
    choose_content_types = State()
    enter_text = State()
    enter_media = State()
    enter_button = State()
    confirm = State()


class GroupSG(StatesGroup):
    list = State()
    add_link = State()


class AccountSG(StatesGroup):
    list = State()
    add_phone = State()
    add_code = State()
    add_2fa = State()
    add_session_string = State()


class ProxySG(StatesGroup):
    list = State()
    add = State()


class CategorySG(StatesGroup):
    list = State()
    create_name = State()
    detail = State()
    add_keyword = State()
    delete_keyword = State()
    add_stop_word = State()
    delete_stop_word = State()
