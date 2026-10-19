import assert from "node:assert/strict";
import { test } from "node:test";
import { resolveSearchGame } from "../src/searchGame.ts";

test("the first search uses the game just detected, not stale React state", () => {
  const detected = { appid: "1091500", name: "Hades II" };
  assert.deepEqual(resolveSearchGame(detected, null), detected);
});

test("a later search uses React state when nothing new was detected", () => {
  const state = { appid: "1091500", name: "Hades II" };
  assert.equal(resolveSearchGame(null, state), state);
});

test("there is nothing to search when no game is running", () => {
  assert.equal(resolveSearchGame(null, null), null);
});
