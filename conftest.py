# conftest.py
import sys
import os

root = os.path.dirname(__file__)

sys.path.insert(0, root)
sys.path.insert(0, os.path.join(root, "api"))
sys.path.insert(0, os.path.join(root, "kernel"))
sys.path.insert(0, os.path.join(root, "protocols"))
sys.path.insert(0, os.path.join(root, "runtime"))