from aiogram.fsm.state import State, StatesGroup


class CaptchaSG(StatesGroup):
    waiting_answer = State()


class UserSG(StatesGroup):
    main_menu = State()
    profile = State()
    instruction = State()
    subscription = State()
    categories_request = State()
    categories_offer = State()


class SubscriptionSG(StatesGroup):
    choose_plan = State()
    choose_payment = State()
    waiting_crypto_payment = State()
