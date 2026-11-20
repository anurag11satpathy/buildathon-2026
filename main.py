"""Tic Tac Toe: play against a friend or an unbeatable computer."""

LINES = [(0, 1, 2), (3, 4, 5), (6, 7, 8),
         (0, 3, 6), (1, 4, 7), (2, 5, 8),
         (0, 4, 8), (2, 4, 6)]


def winner(board):
    for a, b, c in LINES:
        if board[a] != " " and board[a] == board[b] == board[c]:
            return board[a]
    return None


def is_full(board):
    return " " not in board


def show(board):
    print()
    for r in range(3):
        row = [board[r * 3 + c] if board[r * 3 + c] != " " else str(r * 3 + c + 1)
               for c in range(3)]
        print(" " + " | ".join(row))
        if r < 2:
            print("---+---+---")
    print()


def minimax(board, player, me):
    """Score a position for `me`: +1 win, 0 draw, -1 loss."""
    w = winner(board)
    if w:
        return 1 if w == me else -1
    if is_full(board):
        return 0
    other = "O" if player == "X" else "X"
    scores = []
    for i in range(9):
        if board[i] == " ":
            board[i] = player
            scores.append(minimax(board, other, me))
            board[i] = " "
    return max(scores) if player == me else min(scores)


def computer_move(board, me):
    other = "O" if me == "X" else "X"
    best, best_score = None, -2
    for i in range(9):
        if board[i] == " ":
            board[i] = me
            score = minimax(board, other, me)
            board[i] = " "
            if score > best_score:
                best, best_score = i, score
    return best


def ask_move(board, player):
    while True:
        raw = input(f"Player {player}, choose a square (1-9): ").strip()
        if raw.isdigit() and 1 <= int(raw) <= 9 and board[int(raw) - 1] == " ":
            return int(raw) - 1
        print("Invalid move, try again.")


def play(vs_computer):
    board = [" "] * 9
    player = "X"
    while True:
        show(board)
        if vs_computer and player == "O":
            move = computer_move(board, "O")
            print(f"Computer plays {move + 1}")
        else:
            move = ask_move(board, player)
        board[move] = player
        w = winner(board)
        if w:
            show(board)
            print(f"{w} wins!")
            return
        if is_full(board):
            show(board)
            print("It's a draw!")
            return
        player = "O" if player == "X" else "X"


def main():
    print("TIC TAC TOE")
    mode = ""
    while mode not in ("1", "2"):
        mode = input("1) vs computer  2) two players: ").strip()
    while True:
        play(vs_computer=(mode == "1"))
        if input("Play again? (y/n): ").strip().lower() != "y":
            break


if __name__ == "__main__":
    main()
