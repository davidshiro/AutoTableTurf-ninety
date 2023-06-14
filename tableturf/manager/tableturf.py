import copy
import random
from datetime import datetime
from time import sleep
from typing import List, Optional

import cv2
import numpy as np

from capture import Capture
from controller import Controller
from logger import logger
from tableturf.ai import AI
from tableturf.manager import action
from tableturf.manager import detection
from tableturf.manager.closer import Closer, TaskStatsCloser, UnionCloser
from tableturf.manager.data import TaskStats, Profile, JobStats, Result
from tableturf.manager.detection.debugger import Debugger
from tableturf.model import Status, Card, Step, Stage, Grid


class TableTurfManager:
    @staticmethod
    def __resize(capture_fn):
        """
        Resize the captured image to (1920, 1080) to ensure that ROIs work correctly.
        """

        def wrapper():
            img = capture_fn()
            height, width, _ = img.shape
            if height != 1080 or width != 1920:
                img = cv2.resize(img, (1920, 1080))
            return img

        return wrapper

    @staticmethod
    def __equal(a, b) -> bool:
        if isinstance(a, np.ndarray) and isinstance(b, np.ndarray):
            return np.all(a == b)
        else:
            return a == b

    def __multi_detect(self, detect_fn, sleep_time=0.1, max_loop=50):
        def wrapper(*args, **kwargs):
            previous = detect_fn(self.__capture(), *args, **kwargs)
            for _ in range(max_loop):
                sleep(sleep_time)
                current = detect_fn(self.__capture(), *args, **kwargs)
                if isinstance(previous, tuple) and isinstance(current, tuple):
                    if len(previous) == len(current) and np.all([self.__equal(a, b) for a, b in zip(previous, current)]):
                        return current
                elif current == previous:
                    return current
                previous = current
            logger.warn(f'tableturf.multi_detect: exceeded the maximum number of loops')
            return previous

        return wrapper

    def __init__(self, capture: Capture, controller: Controller, ai: AI, debugger: Optional[Debugger] = None):
        self.__capture = self.__resize(capture.capture)
        self.__controller = controller
        self.__ai = ai
        self.__debugger = debugger
        self.job_stats = JobStats()
        self.__session = dict()


    #checks if game is over prematurely
    def __over_check(self, stage_grid):
        logger.debug(f'tableturf.__over_check running on {stage_grid}')
        empty_count = 0
        count = 0
        for i in stage_grid:
            for j in i:
                count += 1
                if j == 64:
                    empty_count += 1
        logger.debug(f'tableturf.__over_check found {empty_count} out of {count} empty squares (threshhold = {.6*count})')
        if (empty_count > (.6*count)):
            return True
        return

    def run(self, profile: Profile, closer: Closer = None, debug=False):
        self.__session = {
            'debug': self.__debugger if debug else None,
        }
        self.job_stats = JobStats()
        for task in profile.tasks:
            if task.current_level < task.target_level or (task.current_level == task.target_level and task.current_win < task.target_win):
                self.__start()
            current_level = task.current_level
            current_win = task.current_win
            while current_level < task.target_level:
                if current_level < 3:
                    to_win = 3 - current_win
                    if to_win > 0:
                        task_closer = TaskStatsCloser(max_win=to_win)
                        if closer is not None:
                            task_closer = UnionCloser(closer, task_closer)
                        self.run_once(task.deck, closer=task_closer, debug=debug)
                        if closer.close(self.job_stats):
                            return
                        self.__switch_level()
                current_level += 1
                current_win = 0
            if current_level == task.target_level:
                to_win = task.target_win - current_win
                if to_win > 0:
                    task_closer = TaskStatsCloser(max_win=to_win)
                    if closer is not None:
                        task_closer = UnionCloser(closer, task_closer)
                    self.run_once(task.deck, closer=task_closer, debug=debug)
                    if closer.close(self.job_stats):
                        return
            self.__switch_npc()
            self.job_stats.task_id += 1

    def run_once(self, deck: int, stage: Optional[Stage] = None, his_deck: Optional[List[Card]] = None, closer: Closer = Closer(), debug=False):
        self.__session = {
            'empty_stage': stage,
            'his_deck': his_deck,
            'debug': self.__debugger if debug else None,
        }
        self.job_stats.task_stats = TaskStats()
        self.job_stats.task_stats.start_time = datetime.now().timestamp()
        while True:
            self.__init_battle()
            self.__select_deck(deck)
            self.__redraw()
            self.__init_roi()
            for round in range(12, 0, -1):
                logger.debug(f'AI sees turn {round}')
                status = self.__get_status(round)
                if self.__over_check(status.stage.grid):
                    break
                step = self.__ai.next_step(status)
                force_restart = self.__move(status, step)
                if force_restart:
                    break
            self.__update_stats()
            close = closer.close(self.job_stats)
            self.__close(close)
            if close:
                break

    def __init_battle(self):
        self.__ai.reset()

    def __select_deck(self, deck: int):
        target = deck
        for i in range(50):
            current = self.__multi_detect(detection.deck_cursor)(debug=self.__session['debug'])
            if current == target:
                break
            if current != -1:
                macro = action.move_deck_cursor_marco(target, current)
                self.__controller.macro(macro)
            else:
                sleep(0.5)
        deck = self.__multi_detect(detection.deck)(debug=self.__session['debug'])
        self.__session['my_deck'] = deck
        self.__controller.press_buttons([Controller.Button.A])
        self.__controller.press_buttons([Controller.Button.A])  # in case command is lost

    def __redraw(self):
        while self.__multi_detect(detection.redraw_cursor)(debug=self.__session['debug']) == -1:
            sleep(0.5)
        hands = self.__multi_detect(detection.hands)(debug=self.__session['debug'])
        stage = self.__session['empty_stage']
        my_deck, his_deck = self.__session['my_deck'], self.__session['his_deck']
        my_remaining_deck = copy.deepcopy(my_deck)
        for card in hands:
            try:
                my_remaining_deck.remove(card)
            except ValueError:
                pass
        redraw = self.__ai.redraw(hands, stage, my_remaining_deck, his_deck)
        target = 1 if redraw else 0
        for i in range(30):
            current = self.__multi_detect(detection.redraw_cursor)(debug=self.__session['debug'])
            if current == target:
                break
            macro = action.move_redraw_cursor_marco(target, current)
            self.__controller.macro(macro)
        self.__controller.press_buttons([Controller.Button.A])
        self.__controller.press_buttons([Controller.Button.A])  # in case command is lost

    def __init_roi(self):
        while self.__multi_detect(detection.hands_cursor)(debug=self.__session['debug']) == -1:
            sleep(0.5)
        rois, roi_width, roi_height = detection.stage_rois(self.__capture(), debug=self.__session['debug'])
        self.__session['rois'] = rois
        self.__session['roi_width'] = roi_width
        self.__session['roi_height'] = roi_height
        self.__session['last_stage'] = None
        stage = self.__multi_detect(detection.stage)(rois=rois, roi_width=roi_width, roi_height=roi_height, last_stage=None, debug=self.__session['debug'])
        self.__session['empty_stage'] = stage

    def __get_status(self, round: int) -> Status:
        my_deck, his_deck = self.__session['my_deck'], self.__session['his_deck']
        while self.__multi_detect(detection.hands_cursor)(debug=self.__session['debug']) == -1:
            # TODO: update his deck here
            sleep(0.5)
        rois, roi_width, roi_height, last_stage = self.__session['rois'], self.__session['roi_width'], self.__session['roi_height'], self.__session['last_stage']
        stage = self.__multi_detect(detection.stage, 0.1, 5)(rois=rois, roi_width=roi_width, roi_height=roi_height, last_stage=last_stage, debug=self.__session['debug']) #BUG: repeats a lot on no cards playable boards
        logger.debug("tableturf.__get_status: running")
        self.__session['last_stage'] = stage
        if self.__over_check(stage.grid):
            return Status(stage=stage, hands=None, round=None, my_sp=None, his_sp=None, my_deck=None, his_deck=None)
        hands = self.__multi_detect(detection.hands)(debug=self.__session['debug'])
        for card in hands:
            try:
                my_deck.remove(card)
            except ValueError:
                pass
        self.__session['my_deck'] = my_deck
        my_sp, his_sp = self.__multi_detect(detection.sp)(debug=self.__session['debug'])
        return Status(stage=stage, hands=hands, round=round, my_sp=my_sp, his_sp=his_sp, my_deck=my_deck, his_deck=his_deck)

    def __move_hands_cursor(self, target):
        for i in range(20):
            current = self.__multi_detect(detection.hands_cursor)(debug=self.__session['debug'])
            if current == target:
                return
            macro = action.move_hands_cursor_marco(target, current)
            self.__controller.macro(macro)
        logger.debug("tableturf.__move_hands_cursor failure")
        return True

    def __move(self, status: Status, step: Step) -> Optional[bool]:
        if step.action == step.Action.Skip:
            return self.__pass(status.hands.index(step.card))

        if step.action == step.Action.SpecialAttack:
            if self.__move_hands_cursor(5):
                logger.debug("tableturf.__move failure")
                return True
            while not self.__multi_detect(detection.special_on)(debug=self.__session['debug']):
                self.__controller.press_buttons([Controller.Button.B]) #prevents weird scenarios where detection fails to realize special is already on
                self.__controller.press_buttons([Controller.Button.A])
        # select card
        if self.__move_hands_cursor(status.hands.index(step.card)):
                logger.debug("tableturf.__move failure")
                return True
        expected_preview = step.card.get_pattern(0)
        for x in range(31):
            if x == 30:
                return self.__pass(status.hands.index(step.card))
            self.__controller.press_buttons([Controller.Button.A])
            preview, current_index = self.__multi_detect(detection.preview)(stage=status.stage, rois=self.__session['rois'], roi_width=self.__session['roi_width'], roi_height=self.__session['roi_height'], debug=self.__session['debug'])
            if action.compare_pattern(preview, expected_preview):
                break
        # rotate card
        if step.rotate > 0:
            target_rotate = step.rotate
            all_patterns = [step.card.get_pattern(i) for i in range(4)]
            for x in range(11):
                if x == 10:
                    logger.debug("tableturf.__move failure: could not find card after rotation")
                    return self.__pass(status.hands.index(step.card))
                actual, _ = self.__multi_detect(detection.preview)(stage=status.stage, rois=self.__session['rois'], roi_width=self.__session['roi_width'], roi_height=self.__session['roi_height'], debug=self.__session['debug'])
                current_rotate = np.argmax([pattern == actual for pattern in all_patterns])
                if current_rotate == 0 and all_patterns[0] != actual:
                    current_rotate = np.argmax([action.compare_pattern(pattern, actual) for pattern in all_patterns])
                rotate = (target_rotate + 4 - current_rotate) % 4
                logger.debug(f'tableturf.rotate: current_rotate={current_rotate}, target_rotate={target_rotate}, step={rotate}')
                if rotate == 0:
                    break
                macro = action.rotate_card_marco(rotate)
                self.__controller.macro(macro)
                # moves card up and left to help CV recognize preview
                self.__controller.press_buttons([Controller.Button.DPAD_UP])
                self.__controller.press_buttons([Controller.Button.DPAD_LEFT])
        # move card
        expected_preview = step.card.get_pattern(step.rotate)
        # in case missing Button.A command
        for x in range(10):
            # keep moving until preview is in the target position
            for y in range(10):
                # keep detecting until preview is found
                for z in range(10):
                    preview, current_index = self.__multi_detect(detection.preview)(stage=status.stage, rois=self.__session['rois'], roi_width=self.__session['roi_width'], roi_height=self.__session['roi_height'], debug=self.__session['debug'])
                    if action.compare_pattern(preview, expected_preview):
                        break
                macro = action.move_card_marco(current_index, preview, status.stage, step)
                if macro.strip() != '':
                    self.__controller.macro(macro)
                else:
                    break
            self.__controller.press_buttons([Controller.Button.A])
            sleep(3)
            # flow didn't go ahead -> card was not placed -> fixes common mistake of 1 too high or 1 too left -> passes
            for i in range(8):
                if status.round == 1:
                    preview, _ = self.__multi_detect(detection.preview)(stage=status.stage, rois=self.__session['rois'], roi_width=self.__session['roi_width'], roi_height=self.__session['roi_height'], debug=self.__session['debug'])
                    if preview is None or np.all(preview.squares == Grid.MySpecial.value):
                        return
                elif self.__multi_detect(detection.hands_cursor)(debug=self.__session['debug']) != -1:
                    return
                sleep(0.5)
            logger.warn("tableturf.move: card placement correcting")
            self.__controller.press_buttons([Controller.Button.DPAD_DOWN])
            self.__controller.press_buttons([Controller.Button.A])
            sleep(0.1)
            self.__controller.press_buttons([Controller.Button.DPAD_UP])
            self.__controller.press_buttons([Controller.Button.DPAD_RIGHT])
            self.__controller.press_buttons([Controller.Button.A])
            sleep(0.1)
            self.__controller.press_buttons([Controller.Button.DPAD_LEFT])
            sleep(0.3)

            for i in range(3):
                if status.round == 1:
                    preview, _ = self.__multi_detect(detection.preview)(stage=status.stage, rois=self.__session['rois'], roi_width=self.__session['roi_width'], roi_height=self.__session['roi_height'], debug=self.__session['debug'])
                    if preview is None or np.all(preview.squares == Grid.MySpecial.value):
                        return
                elif self.__multi_detect(detection.hands_cursor)(debug=self.__session['debug']) != -1:
                    return
                sleep(0.5)

            return self.__pass(status.hands.index(step.card))
        return True

    def __pass(self, card):
        self.__controller.press_buttons([Controller.Button.B])  #make sure we are not in card placement
        if self.__move_hands_cursor(4):
            logger.debug("tableturf.__move failure")
            return True
        while not self.__multi_detect(detection.skip)(debug=self.__session['debug']):
            self.__controller.press_buttons([Controller.Button.A])
        if self.__move_hands_cursor(card):
            logger.debug("tableturf.__move failure")
            return True
        self.__controller.press_buttons([Controller.Button.A])
        self.__controller.press_buttons([Controller.Button.A])  # in case command is lost
        return

    def __update_stats(self):
        sleep(10)
        result = self.__multi_detect(detection.result)(debug=self.__session['debug'])
        if result == Result.Win:
            self.job_stats.task_stats.win += 1
        now = datetime.now().timestamp()
        self.job_stats.time = now - self.job_stats.task_stats.start_time
        self.job_stats.task_stats.battle += 1
        logger.debug(f'tableturf.update_stats: stats={self.job_stats}')

    def __close(self, close: bool):
        self.__controller.press_buttons([Controller.Button.A])
        target = 0 if close else 1
        count = 0
        for i in range(31):
            current = self.__multi_detect(detection.replay_cursor)(debug=self.__session['debug'])
            if current == target:
                break
            if current != -1:
                macro = action.move_replay_cursor_marco(target, current)
                self.__controller.macro(macro)
            else:
                sleep(0.5)
            # press A when unlock new items
            count = (count + 1) % 6
            if count == 0:
                self.__controller.press_buttons([Controller.Button.A])
        self.__controller.press_buttons([Controller.Button.A])
        self.__controller.press_buttons([Controller.Button.A])  # in case command is lost

    def __start(self):
        while not self.__multi_detect(detection.level)(debug=self.__session['debug']):
            self.__controller.press_buttons([Controller.Button.A])
            sleep(2)
        self.__controller.press_buttons([Controller.Button.DPAD_LEFT])
        while not self.__multi_detect(detection.start)(debug=self.__session['debug']):
            self.__controller.press_buttons([Controller.Button.DPAD_DOWN])
            sleep(0.5)
        self.__controller.press_buttons([Controller.Button.A])
        self.__controller.press_buttons([Controller.Button.A])  # in case command is lost
        sleep(2)
        # while self.__multi_detect(detection.deck_cursor)(debug=self.__session['debug']) == -1:
        #     self.__controller.press_buttons([Controller.Button.A])
        #     sleep(0.5)

    def __switch_level(self):
        sleep(3)
        while not self.__multi_detect(detection.level)(debug=self.__session['debug']):
            self.__controller.press_buttons([Controller.Button.A])
            sleep(2)
        self.__controller.press_buttons([Controller.Button.DPAD_LEFT])
        while not self.__multi_detect(detection.start)(debug=self.__session['debug']):
            self.__controller.press_buttons([Controller.Button.DPAD_DOWN])
            sleep(0.5)
        self.__controller.press_buttons([Controller.Button.A])
        self.__controller.press_buttons([Controller.Button.A])  # in case command is lost
        sleep(2)
        # while self.__multi_detect(detection.deck_cursor)(debug=self.__session['debug']) == -1:
        #     self.__controller.press_buttons([Controller.Button.A])
        #     sleep(0.5)

    def __switch_npc(self):
        sleep(3)
        while not self.__multi_detect(detection.level)(debug=self.__session['debug']):
            self.__controller.press_buttons([Controller.Button.A])
            sleep(2)
        self.__controller.press_buttons([Controller.Button.B])
        self.__controller.press_buttons([Controller.Button.B])  # in case command is lost
        sleep(2)
        self.__controller.press_buttons([Controller.Button.DPAD_DOWN])
