import json
import re


def _exhaust(gen):
    try:
        while True:
            next(gen)
    except StopIteration as e:
        return e.value


class AutoReplySupervisor:
    def __init__(self, llmclients, llm_no=0, auto_cycle=False):
        if not llmclients:
            raise ValueError('No llm clients available for auto supervisor.')
        self.llmclients = llmclients
        self.llm_no = llm_no % len(llmclients)
        self.llmclient = self.llmclients[self.llm_no]
        self.auto_cycle = auto_cycle

    def get_llm_name(self):
        b = self.llmclient.backend
        return f"{type(b).__name__}/{b.name}"

    def _reset_history(self):
        if hasattr(self.llmclient.backend, 'history'):
            self.llmclient.backend.history = []

    def _ask_llm(self, system, user):
        self._reset_history()
        resp = _exhaust(self.llmclient.chat([
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]))
        return self._clean_reply(getattr(resp, 'content', '') if resp is not None else '')

    def _clean_reply(self, text):
        text = (text or '').strip()
        if text.startswith('```'):
            lines = text.splitlines()
            if len(lines) >= 3 and lines[-1].strip().startswith('```'):
                text = '\n'.join(lines[1:-1]).strip()
        text = re.sub(r'^\s*(reply|input|message)\s*:\s*', '', text, flags=re.I)
        return text.strip().strip('"').strip()

    def _parse_json_object(self, text):
        text = (text or '').strip()
        if not text:
            return {}
        fenced = re.search(r'```(?:json)?\s*(\{.*\})\s*```', text, re.DOTALL | re.I)
        if fenced:
            text = fenced.group(1).strip()
        try:
            data = json.loads(text)
            return data if isinstance(data, dict) else {}
        except Exception:
            pass
        match = re.search(r'(\{.*\})', text, re.DOTALL)
        if not match:
            return {}
        try:
            data = json.loads(match.group(1))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _normalize_ask_user_payload(self, payload):
        if not isinstance(payload, dict):
            return {}
        question = str(payload.get('question', '') or '').strip()
        candidates = payload.get('candidates') or []
        if not isinstance(candidates, list):
            candidates = [candidates]
        return {'question': question, 'candidates': candidates} if question else {}

    def _extract_ask_user_payload(self, loop_result):
        if not isinstance(loop_result, dict):
            return {}
        candidates = [loop_result.get('ask_user')]
        data = loop_result.get('data')
        if isinstance(data, dict):
            candidates.extend([data, data.get('ask_user'), data.get('data')])
        for payload in candidates:
            normalized = self._normalize_ask_user_payload(payload)
            if normalized:
                return normalized
        return {}

    def _extract_ask_user_payload_from_text(self, output_text):
        text = output_text or ''
        inline_matches = re.findall(r'ask_user\((\{.*?\})\)', text, flags=re.DOTALL)
        for snippet in reversed(inline_matches):
            payload = self._normalize_ask_user_payload(self._parse_json_object(snippet))
            if payload:
                return payload
        block_matches = re.findall(
            r'Tool:\s*`ask_user`.*?`{3,}(?:text|json)?\s*(\{.*?\})\s*`{3,}',
            text,
            flags=re.DOTALL,
        )
        for snippet in reversed(block_matches):
            payload = self._normalize_ask_user_payload(self._parse_json_object(snippet))
            if payload:
                return payload
        return {}

    def _extract_question(self, loop_result, output_text):
        payload = self._extract_ask_user_payload(loop_result)
        if payload:
            return payload.get('question', ''), payload.get('candidates') or []
        payload = self._extract_ask_user_payload_from_text(output_text)
        if payload:
            return payload.get('question', ''), payload.get('candidates') or []
        return self._extract_natural_questions(output_text), []

    def _extract_natural_questions(self, output_text):
        text = output_text or ''
        text = re.sub(r'<thinking>.*?</thinking>', '', text, flags=re.DOTALL | re.I)
        text = re.sub(r'<summary>.*?</summary>', '', text, flags=re.DOTALL | re.I)
        text = re.sub(r'```.*?```', '', text, flags=re.DOTALL)
        prompt_tokens = ('请先确认', '请先回答', '你想要什么', '你希望', '是否需要', '只需回答')
        lines = []
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            line = re.sub(r'^\s*(?:[-*]|\d+[\.\)\u3001\uFF09])\s*', '', line)
            if not line:
                continue
            if 'Tool:' in line or '`ask_user`' in line or 'Waiting for your answer' in line:
                continue
            if '?' in line or '\uFF1F' in line:
                lines.append(line)
                continue
            if any(token in line for token in prompt_tokens):
                lines.append(line)
        if not lines:
            return ''
        return '\n'.join(lines[:5])

    def _generate_human_answer(self, last_input, output_text, loop_result, question, candidates):
        result_name = (loop_result or {}).get('result', 'UNKNOWN')
        system = (
            "You are a supervisor for an autonomous agent.\n"
            "Your only job is to generate the user reply that should be sent back to the main agent.\n"
            "Output only the reply text itself. Do not explain. Do not use markdown.\n"
            "Answer the agent's question directly, using the provided candidates when useful.\n"
            "Keep it short and concrete."
        )
        user = (
            f"Last user input:\n{last_input}\n\n"
            f"Loop result:\n{result_name}\n\n"
            f"Assistant output:\n{output_text[-12000:]}\n\n"
            f"Question to answer:\n{question}\n\n"
        )
        if candidates:
            user += f"Candidates:\n{json.dumps(candidates, ensure_ascii=False)}\n\n"
        user += "Return the reply text only."
        text = self._ask_llm(system, user)
        if text:
            return text
        return candidates[0] if candidates else f"按你的判断继续，问题是：{question}"

    def _generate_autonomous_input(self, last_input, output_text, loop_result):
        result_name = (loop_result or {}).get('result', 'UNKNOWN')
        system = (
            "You are a supervisor for an autonomous agent.\n"
            "Generate the next user input that should be sent to the main agent so it can keep moving.\n"
            "Output only the next input text itself. Do not explain. Do not pretend to be answering ask_user.\n"
            "Prefer a short, concrete instruction.\n"
            "If the previous result is CURRENT_TASK_DONE, move it into the next meaningful action or review.\n"
            "If the previous result is MAX_TURNS_EXCEEDED, first ask it to summarize the blocker and then continue with a new strategy."
        )
        user = (
            f"Last user input:\n{last_input}\n\n"
            f"Loop result:\n{result_name}\n\n"
            f"Assistant output:\n{output_text[-12000:]}\n\n"
            "Generate the next input."
        )
        text = self._ask_llm(system, user)
        if text:
            return text
        if result_name == 'MAX_TURNS_EXCEEDED':
            return "[AUTO] 先读取 autonomous_operation_sop 和当前工作记忆，总结上一轮卡住的原因，换一种策略继续推进；如果没有高价值继续路径，就转入自主任务规划。"
        return "[AUTO] 先读取 autonomous_operation_sop，检查上一轮结果是否还有遗漏；若当前任务已完成，就按自主行动 SOP 选择一个新的高价值 TODO 并执行。"

    def should_auto_stop(self, last_input, output_text):
        system = (
            "You are a supervisor for an autonomous agent UI.\n"
            "Decide whether the current task is truly complete and the UI should disable auto-follow.\n"
            "Return JSON only: {\"stop\": true/false, \"reason\": \"short reason\"}.\n"
            "Use stop=true only when the assistant has already completed the user's requested task or clearly states that no further action is needed.\n"
            "If the assistant is asking questions, waiting for confirmation, proposing next steps, or obvious follow-up work remains, return stop=false."
        )
        user = (
            f"Last user input:\n{(last_input or '')[-6000:]}\n\n"
            f"Assistant reply:\n{(output_text or '')[-12000:]}\n\n"
            "Should the UI auto-stop now?"
        )
        payload = self._parse_json_object(self._ask_llm(system, user))
        stop = bool(payload.get('stop', False))
        reason = str(payload.get('reason', '') or '').strip()
        return stop, reason or ('llm_decision_stop' if stop else 'llm_decision_continue')

    def generate_next_input(self, last_input, output_text, loop_result=None):
        question, candidates = self._extract_question(loop_result, output_text)
        if question:
            return self._generate_human_answer(last_input, output_text, loop_result, question, candidates), 'ask_user'
        if not self.auto_cycle:
            return None, 'noop'
        return self._generate_autonomous_input(last_input, output_text, loop_result), 'auto_cycle'
