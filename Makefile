SESSION_ID := $(firstword $(filter-out check -- --h5,$(MAKECMDGOALS)))

.PHONY: check
check:
	@python3 check_capture.py "$(SESSION_ID)"

%:
	@:
