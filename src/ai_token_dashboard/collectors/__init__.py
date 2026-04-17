"""Collectors: pluggable readers for each AI tool.

Each collector class produces ``TokenEvent`` instances and hands them to the
Database + EventBus. File-watchers run in their own thread using ``watchdog``;
API pollers run in the asyncio loop.
"""
