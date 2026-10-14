import os
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import lbmetrics  # noqa: E402


def spin(seconds):
    end = time.process_time() + seconds
    while time.process_time() < end:
        pass


class CpuTest(unittest.TestCase):
    def test_resolution_is_finer_than_a_clock_tick(self):
        # 2ms of work is a fifth of a 10ms tick: invisible to utime+stime,
        # visible to schedstat.
        before = lbmetrics.thread_cpu_seconds(os.getpid())
        spin(0.002)
        after = lbmetrics.thread_cpu_seconds(os.getpid())
        self.assertGreater(after - before, 0.001)

    def test_process_total_includes_other_threads(self):
        # Read while the worker is still alive: an exited thread's
        # /proc/<pid>/task entry, and its time, are gone.
        done, release = threading.Event(), threading.Event()

        def work():
            spin(0.05)
            done.set()
            release.wait()

        before = lbmetrics.process_cpu_seconds(os.getpid())
        main_before = lbmetrics.thread_cpu_seconds(os.getpid())
        t = threading.Thread(target=work)
        t.start()
        done.wait()
        gained = lbmetrics.process_cpu_seconds(os.getpid()) - before
        main_gained = lbmetrics.thread_cpu_seconds(os.getpid()) - main_before
        release.set()
        t.join()
        self.assertGreater(gained - main_gained, 0.03)

    def test_missing_process_reads_as_none(self):
        self.assertIsNone(lbmetrics.thread_cpu_seconds(2**22 + 12345))
        self.assertIsNone(lbmetrics.process_cpu_seconds(2**22 + 12345))


if __name__ == "__main__":
    unittest.main()
