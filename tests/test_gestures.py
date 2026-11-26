import math
import unittest

from jarvis.gestures import Gesture, GestureInterpreter, classify_gesture


def hand(raised=(True, False, False, False), pinch=False):
    points = [(0.5, 0.75)] * 21
    points[0] = (0.5, 0.9)
    points[4] = (0.2, 0.55)
    for i, (mcp, pip, dip, tip) in enumerate(((5, 6, 7, 8), (9, 10, 11, 12), (13, 14, 15, 16), (17, 18, 19, 20))):
        x = 0.38 + i * 0.08
        points[mcp] = (x, 0.7)
        points[pip] = (x, 0.5)
        points[dip] = (x, 0.35 if raised[i] else 0.64)
        points[tip] = (x, 0.2 if raised[i] else 0.74)
    if pinch:
        points[4] = (points[8][0] - 0.015, points[8][1])
    return points


class ClassifierTests(unittest.TestCase):
    def test_index_moves_and_two_fingers_scroll(self):
        self.assertEqual(classify_gesture(hand()).name, "move")
        self.assertEqual(classify_gesture(hand((True, True, False, False))).name, "scroll")

    def test_palm_fist_and_missing_hand_pause(self):
        self.assertEqual(classify_gesture(hand((True,) * 4)).name, "pause")
        self.assertEqual(classify_gesture(hand((False,) * 4)).name, "pause")
        self.assertEqual(classify_gesture(None).name, "none")
        points = hand()
        points[8] = (math.nan, 0.5)
        self.assertEqual(classify_gesture(points).name, "none")

    def test_pinch(self):
        self.assertEqual(classify_gesture(hand(pinch=True)).name, "pinch")

    def test_pose_classification_is_independent_of_left_or_right_hand(self):
        for raised, expected in (((True, False, False, False), "move"), ((True, True, False, False), "scroll")):
            mirrored = [(1 - x, y) for x, y in hand(raised)]
            self.assertEqual(classify_gesture(mirrored).name, expected)


class InterpreterTests(unittest.TestCase):
    def test_unarmed_never_emits_actions(self):
        interpreter = GestureInterpreter()
        for n in range(30):
            self.assertEqual(interpreter.update(Gesture("move", 0.8, 0.2), n, armed=False), [])
            self.assertEqual(interpreter.update(Gesture("pinch"), n, armed=False), [])

    def test_click_requires_recent_move_and_does_not_repeat(self):
        interpreter = GestureInterpreter()
        self.assertEqual(interpreter.update(Gesture("pinch"), 0), [])
        self.assertEqual(interpreter.update(Gesture("pinch"), 0.2), [])
        interpreter.update(Gesture("move"), 1)
        self.assertEqual(interpreter.update(Gesture("move"), 1.2)[0][0], "move")
        self.assertEqual(interpreter.update(Gesture("pinch"), 1.3), [])
        self.assertEqual(interpreter.update(Gesture("pinch"), 1.5), [("click",)])
        self.assertEqual(interpreter.update(Gesture("pinch"), 2.0), [])

    def test_scroll_has_deadzone_and_bounded_steps(self):
        interpreter = GestureInterpreter()
        self.assertEqual(interpreter.update(Gesture("scroll", y=0.7), 1), [])
        self.assertEqual(interpreter.update(Gesture("scroll", y=0.7), 1.2), [])
        self.assertEqual(interpreter.update(Gesture("scroll", y=0.69), 1.3), [])
        self.assertEqual(interpreter.update(Gesture("scroll", y=0.1), 1.4), [("scroll", 4)])

    def test_cursor_stays_in_bounds_and_smoothing_limits_jump(self):
        interpreter = GestureInterpreter()
        interpreter.reset((0.5, 0.5))
        interpreter.update(Gesture("move", 9, -9), 1)
        action = interpreter.update(Gesture("move", 9, -9), 1.2)[0]
        self.assertAlmostEqual(action[1], 0.66)
        self.assertAlmostEqual(action[2], 0.34)

    def test_disarming_forgets_previous_click_eligibility(self):
        interpreter = GestureInterpreter()
        interpreter.update(Gesture("move"), 1)
        interpreter.update(Gesture("move"), 1.2)
        interpreter.update(Gesture("pinch"), 1.25, armed=False)
        interpreter.update(Gesture("pinch"), 1.3)
        self.assertEqual(interpreter.update(Gesture("pinch"), 1.5), [])


if __name__ == "__main__":
    unittest.main()
