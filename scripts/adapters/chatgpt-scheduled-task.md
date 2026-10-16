# ChatGPT scheduled task

Create this as a ChatGPT scheduled task in the desktop app, with this repository attached as a local project. The task cannot see `.ai-collab` unless that folder is attached. Replace `backend` with the participant name the human chose. Do not use `chatgpt` or `gpt` as the name unless the human actually named the participant that.

```text
You are participant `backend` in the local repository this task is attached to. Run:

python /ABSOLUTE/PATH/TO/agent-collaboration-skill/scripts/collab.py inbox --root . --agent backend --format text

If the first line is `needs_attention: no`, stop immediately. Do not edit files and do not create another task.
If the first line is `needs_attention: yes`, read SKILL.md in the agent-collaboration skill and process only the first listed message, one cycle. If `coordinator=yes` or `may_edit_code=no`, do not create, modify, or delete product code, configuration, or tests. If `may_commit=no`, do not run git commit or git push. Publish the reply with --in-message-id, or acknowledge a terminal message, then stop. Do not start a watcher, a loop, or another scheduled task.
```

Replace `/ABSOLUTE/PATH/TO/agent-collaboration-skill` with the skill checkout on this computer. Run the task only while this computer is awake and the project folder is available.
