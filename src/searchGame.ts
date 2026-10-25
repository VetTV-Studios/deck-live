export type RunningGame = { appid: string; name: string };

/**
 * Game a search must use. Pass the game just read from Steam: setState has not
 * flushed yet, so the React state value is still null on that first call.
 * Later calls pass null for `detected` and use the state game.
 */
export function resolveSearchGame(
  detected: RunningGame | null,
  stateGame: RunningGame | null,
): RunningGame | null {
  return detected ?? stateGame;
}
