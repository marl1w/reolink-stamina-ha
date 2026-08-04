"""Pushing event clips to a cloud destination.

Split so that the parts worth testing are testable without a recorder, a cloud account or a
running Home Assistant:

* `naming` decides what a clip is called and where it goes.
* `index` records what has been uploaded and works out what must go to make room.
* `windows` turns a burst of detections into one clip worth keeping.
* `destination` is the interface a cloud has to satisfy, and its implementations.
"""
