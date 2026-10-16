# ChatGPT scheduled task

Create this as a ChatGPT scheduled task in the desktop app, with this repository attached as a local project. The task cannot see `.ai-collab` unless that folder is attached. Use the same participant id that the thread lists for you. The example below uses `chatgpt`; change it if the thread calls you `codex`.

```text
You are participant `chatgpt` in the local repository this task is attached to. Run:

python /ABSOLUTE/PATH/TO/agent-collaboration-skill/scripts/collab.py inbox --root . --agent chatgpt --format text

If the first line is `needs_attention: no`, stop immediately. Do not edit files and do not create another task.
If the first line is `needs_attention: yes`, read SKILL.md in the agent-collaboration skill and process only the first listed message, one cycle. Publish the reply with --in-message-id, or acknowledge a terminal message, then stop. Do not start a watcher, a loop, or another scheduled task.
```

Replace `/ABSOLUTE/PATH/TO/agent-collaboration-skill` with the skill checkout on this computer. Run the task only while this computer is awake and the project folder is available.
