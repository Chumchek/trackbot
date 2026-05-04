import logging
import re

from trello import TrelloClient

logger = logging.getLogger(__name__)


class TrelloAPI:
    def __init__(self, api_key, api_secret, token):
        self.client = TrelloClient(
            api_key=api_key,
            api_secret=api_secret,
            token=token
        )

    # Отримати дошку по id
    def get_board(self, board_id):
        try:
            return self.client.get_board(board_id)
        except Exception as e:
            logger.warning("Trello: помилка отримання дошки %s: %s", board_id, e)
            return None

    # Отримати організацію по id
    def get_organization(self, organization_id):
        return self.client.get_organization(organization_id)

    # Отримати організації
    def get_organizations(self):
        return self.client.list_organizations()

    # Отримати дошки для організації
    def get_boards_for_org(self, org):
        return org.get_boards(list_filter='open')

    # Отримати картки для дошки
    def get_cards_for_board(self, board):
        return board.get_cards()

    # Отримати списки на дошці
    def get_lists_for_board(self, board):
        return board.get_lists(list_filter='open')

    def get_cards_for_list(self, list):
        return list.list_cards()

    # Метод для пошуку картки за bundle_id
    def get_card_by_bundle(self, board, bundle_id):
        for card in board.get_cards():
            if bundle_id in card.name:
                return card  
        return None 

    # Метод для отримання app_id з картки
    def get_app_id_from_card(self, card):
        match = re.search(r"\[(.*?)\]", card.name)
        if match:
            return match.group(1)  # Повертаємо app_id
        return None 

    def get_card_and_app_id_by_bundle(self, board, bundle_id):
        card = self.get_card_by_bundle(board, bundle_id) 
        if card:
            app_id = self.get_app_id_from_card(card)
            return card, app_id
        return None, None

    def move_card_to_list(self, card, target_list):
        card.change_list(target_list.id)

    def get_list_by_id(self, board, list_id):
        """Повертає список на дошці за list_id або None."""
        try:
            return board.get_list(list_id)
        except Exception as e:
            logger.warning("Trello: не вдалося отримати список %s: %s", list_id, e)
            return None

    def move_app_card_by_status(
        self,
        board_id: str,
        bundle_id: str,
        available: bool,
        banned_list_id: str,
        in_market_list_id: str,
    ) -> None:
        """
        Знаходить картку за bundle_id, переміщує її у TRELLO_BANNED_LIST_ID
        якщо додаток unavailable, або в TRELLO_IN_MARKET_LIST_ID якщо available.
        """
        if not board_id or not bundle_id:
            logger.info("Trello: пропуск (board_id або bundle_id порожні)")
            return
        target_list_id = banned_list_id if not available else in_market_list_id
        if not target_list_id:
            logger.info("Trello: пропуск (target list id не налаштовано для status=%s)", "available" if available else "unavailable")
            return

        board = self.get_board(board_id)
        if not board:
            logger.warning("Trello: не вдалося отримати дошку %s для bundle_id=%s", board_id, bundle_id)
            return

        card, app_id = self.get_card_and_app_id_by_bundle(board, bundle_id)
        if not card:
            logger.info("Trello: картка не знайдена для bundle_id=%s (дошка %s)", bundle_id, board_id)
            return

        target_list = self.get_list_by_id(board, target_list_id)
        if not target_list:
            logger.warning("Trello: список %s не знайдено на дошці %s", target_list_id, board_id)
            return

        try:
            self.move_card_to_list(card, target_list)
            list_name = "BANNED" if not available else "IN_MARKET"
            logger.info(
                "Trello: картку '%s' (bundle_id=%s, app_id=%s) переміщено в список %s",
                card.name,
                bundle_id,
                app_id or "—",
                list_name,
            )
        except Exception as e:
            logger.exception(
                "Trello: помилка переміщення картки bundle_id=%s в список %s: %s",
                bundle_id,
                target_list_id,
                e,
            )