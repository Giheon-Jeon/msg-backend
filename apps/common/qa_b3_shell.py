# QA B3: Bandit B602 test only. Do not merge.
import subprocess


def qa_b3_run(user_input):
    return subprocess.call("echo " + user_input, shell=True)
