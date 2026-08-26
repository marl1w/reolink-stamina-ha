"""Pushing event clips off the recorder, to a cloud or to the NAS down the hall.

Split so that the parts worth testing are testable without a recorder, an account or a
running Home Assistant:

* `naming` decides what a clip is called and where it goes.
* `index` records what has been uploaded and works out what must go to make room.
* `windows` turns a burst of detections into one clip worth keeping.
* `destinations` is the interface a provider has to satisfy, and one class per provider.
"""
