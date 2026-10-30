"""Page flows: one function per MontyCloud UI page, replaying its browser request waterfall.

Each flow takes the running MontyCloudUser and uses only user.call / api_get /
run_batch / org_id / label / log, so any scenario can reuse a page.
"""
