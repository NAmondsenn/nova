"""
A stand-in for Approvals, for the tests of what asks it: it records each gate call
and answers as it's set to, without a passkey, a PC or a thread.
"""

from voice_assistant.approvals import NO_WAY, pending_said


class FakeApprovals:
    """
    Answers gate as it's told: "approve" as the trust window open, running the action at once;
    "wait" with the words said while a request waits, keeping the action to be approved later
    by approve_waiting; "none" as there being no way to approve, running nothing.
    """

    def __init__(self, answer="approve"):
        self.answer = answer
        # Each gate call: the description and the satellite its outcome is said on.
        self.asked = []
        # The actions still waiting, as (do, done).
        self.waiting = []

    def gate(self, description, do, satellite, done=None):
        self.asked.append((description, satellite))
        if self.answer == "approve":
            return do()
        if self.answer == "none":
            return {"success": False, "message": NO_WAY}
        self.waiting.append((do, done))
        return {"success": True, "message": pending_said(["your phone"])}

    def approve_waiting(self):
        """Approves each action waiting, as the wait thread would: runs it, then gives done its reply."""
        replies = []
        for do, done in self.waiting:
            reply = do()
            if done is not None:
                done(reply)
            replies.append(reply)
        self.waiting.clear()
        return replies
