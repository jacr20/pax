import numpy as np
from pax.trigger import TriggerPlugin
from pax import units


class GroupTriggers(TriggerPlugin):
    """Find event ranges corresponding to trigger_times,
    grouping nearby triggers and applying left and right extension
    """

    def process(self, data):
        trigger_times = data.signals[data.signals['trigger']]['left_time']
        event_ranges = []
        left_ext = self.config['left_extension']
        right_ext = self.config['right_extension']
        max_l = self.config['max_event_length']
        truncated_events = 0
        dead_time_due_to_truncation = 0

        for group_of_tr in split_on_gap(trigger_times, self.config['event_separation']):
            start = group_of_tr[0] - left_ext
            stop = group_of_tr[-1] + right_ext
            if stop - start > max_l:
                self.log.warning("Event %d-%d too long (%0.2f ms), truncated to %0.2f ms. "
                                 "Consider changing trigger settings!" % (start, stop,
                                                                          (stop - start) / units.ms,
                                                                          max_l / units.ms))
                dead_time_due_to_truncation += stop - start - max_l
                stop = start + max_l
                truncated_events += 1

            event_ranges.append((start, stop))

        data.event_ranges = np.array(event_ranges, dtype=np.int64)

        data.batch_info_doc['truncated_events'] = truncated_events
        data.batch_info_doc['dead_time_due_to_truncation'] = dead_time_due_to_truncation
        self.trigger.end_of_run_info['truncated_events'] += truncated_events
        self.trigger.end_of_run_info['dead_time_due_to_truncation'] += dead_time_due_to_truncation


def split_on_gap(a, threshold):
    """Split a into list of arrays, each separated by threshold or more"""
    if not len(a):
        return []
    if len(a) < 2:
        return [a]
    return np.split(a, np.where(np.diff(a) >= threshold)[0] + 1)
