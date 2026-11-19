import unittest

from agent import gemini


class GeminiFormatTests(unittest.TestCase):
    def test_sanitize_nullable_and_drops_additional_properties(self):
        schema = {"type": "object", "additionalProperties": False, "properties": {"water_mm": {"type": ["number", "null"]}, "tags": {"type": "array", "items": {"type": "string"}}}}
        self.assertEqual(gemini._sanitize(schema), {"type": "object", "properties": {"water_mm": {"type": "number", "nullable": True}, "tags": {"type": "array", "items": {"type": "string"}}}})

    def test_function_declarations_skip_empty_parameters(self):
        decls = gemini.function_declarations([
            {"name": "list_fields", "description": "d", "input_schema": {"type": "object", "properties": {}}},
            {"name": "get_weather", "description": "w", "input_schema": {"type": "object", "properties": {"field_id": {"type": "string"}}}},
        ])
        self.assertNotIn("parametersJsonSchema", decls[0])
        self.assertIn("parametersJsonSchema" if gemini._use_json_schema else "parameters", decls[1])

    def test_turn_helpers_keep_signatures_and_ids(self):
        candidate = {"content": {"role": "model", "parts": [{"functionCall": {"name": "get_weather", "args": {}, "id": "c1"}, "thoughtSignature": "sig"}]}}
        self.assertEqual(gemini.model_turn(candidate)["parts"][0]["thoughtSignature"], "sig")
        reply = gemini.function_response_turn([(gemini.function_calls(candidate)[0], {"ok": True})])
        self.assertEqual(reply, {"role": "user", "parts": [{"functionResponse": {"name": "get_weather", "response": {"ok": True}, "id": "c1"}}]})

    def test_text_ignores_thought_parts(self):
        candidate = {"content": {"parts": [{"text": "thinking...", "thought": True}, {"text": "Cavendish looks good."}]}}
        self.assertEqual(gemini.text_of(candidate), "Cavendish looks good.")


if __name__ == "__main__":
    unittest.main()
