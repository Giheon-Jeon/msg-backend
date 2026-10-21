from django.urls import path

from apps.board.views import (
    AirportMoveView,
    BoardMeView,
    CellCurrentView,
    CellOpenView,
    ChanceCardCatalogView,
    ChanceConfirmView,
    ChanceDiscardView,
    ChanceNowView,
    ChanceUseView,
    DiceConfirmView,
    DiceRollView,
    DiceRollRequestView,
    DiceRollRequestAckView,
    DiceStatusView,
    OpenedChallengesView,
    RouletteSpinView,
)

app_name = "board"

urlpatterns = [
    path("me", BoardMeView.as_view(), name="board-me"),
    path("cell/current", CellCurrentView.as_view(), name="cell-current"),
    path("cell/open", CellOpenView.as_view(), name="cell-open"),
    path("opened_challenges", OpenedChallengesView.as_view(), name="opened-challenges"),
    path("chance/catalog", ChanceCardCatalogView.as_view(), name="chance-catalog"),
    path("chance/now", ChanceNowView.as_view(), name="chance-now"),
    path("chance/use", ChanceUseView.as_view(), name="chance-use"),
    path("chance/confirm", ChanceConfirmView.as_view(), name="chance-confirm"),
    path("chance/discard", ChanceDiscardView.as_view(), name="chance-discard"),
    path("dice/status", DiceStatusView.as_view(), name="dice-status"),
    path("dice/roll", DiceRollView.as_view(), name="dice-roll"),
    path("dice/request", DiceRollRequestView.as_view(), name="dice-request"),
    path("dice/request/ack", DiceRollRequestAckView.as_view(), name="dice-request-ack"),
    path("dice/confirm", DiceConfirmView.as_view(), name="dice-confirm"),
    path("airport/move", AirportMoveView.as_view(), name="airport-move"),
    path("roulette/spin", RouletteSpinView.as_view(), name="roulette-spin"),
]
