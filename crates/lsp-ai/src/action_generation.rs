//! Instruction generation shared by diagnostic and refactoring actions.
use crate::{
    config::{ActionPromptFormat, Kwargs},
    memory_backends::{ContextAndCodePrompt, Prompt},
    transformer_backends::TransformerBackend,
};
use anyhow::Context;
use serde_json::{json, Value};

pub(crate) fn parameters(
    options: &Kwargs,
    format: ActionPromptFormat,
    instruction: &str,
    default_tokens: u64,
    token_cap: Option<u64>,
) -> anyhow::Result<Value> {
    let mut parameters = serde_json::to_value(options)?;
    let parameters_map = parameters
        .as_object_mut()
        .context("Invalid action parameters")?;
    parameters_map
        .entry("max_tokens")
        .or_insert(json!(default_tokens));
    if token_cap.is_some() {
        let max_tokens = parameters_map["max_tokens"]
            .as_u64()
            .unwrap_or(token_cap.unwrap())
            .min(token_cap.unwrap());
        parameters_map.insert("max_tokens".into(), json!(max_tokens));
    }
    parameters_map.entry("temperature").or_insert(json!(0));
    parameters_map.remove("fim");
    match format {
        ActionPromptFormat::Messages => {
            parameters_map.insert(
                "messages".into(),
                json!([
                    {"role": "system", "content": instruction},
                    {"role": "user", "content": "{CODE}"}
                ]),
            );
        }
        ActionPromptFormat::Anthropic => {
            parameters_map.insert("system".into(), json!(instruction));
            parameters_map.insert(
                "messages".into(),
                json!([
                    {"role": "user", "content": "{CODE}"}
                ]),
            );
        }
        ActionPromptFormat::Gemini => {
            parameters_map.insert(
                "systemInstruction".into(),
                json!({
                    "role": "system", "parts": [{"text": instruction}]
                }),
            );
            parameters_map.insert(
                "contents".into(),
                json!([
                    {"role": "user", "parts": [{"text": "{CODE}"}]}
                ]),
            );
            if !parameters_map.contains_key("generationConfig") {
                let max_tokens = parameters_map["max_tokens"].clone();
                let temperature = parameters_map["temperature"].clone();
                parameters_map.insert(
                    "generationConfig".into(),
                    json!({
                        "maxOutputTokens": max_tokens, "temperature": temperature
                    }),
                );
            }
        }
    }
    if token_cap.is_some() {
        if let Some(generation_config) = parameters_map
            .get_mut("generationConfig")
            .and_then(|value| value.as_object_mut())
        {
            let max_tokens = generation_config
                .get("maxOutputTokens")
                .and_then(|value| value.as_u64())
                .unwrap_or(token_cap.unwrap())
                .min(token_cap.unwrap());
            generation_config.insert("maxOutputTokens".into(), json!(max_tokens));
        }
    }

    Ok(parameters)
}

pub(crate) async fn generate(
    backend: &(dyn TransformerBackend + Send + Sync),
    format: ActionPromptFormat,
    parameters: Value,
    payload: Value,
) -> anyhow::Result<String> {
    let prompt = Prompt::ContextAndCode(ContextAndCodePrompt {
        context: String::new(),
        code: payload.to_string(),
        selected_text: None,
    });
    let result = backend
        .do_generate(&prompt, parameters)
        .await?
        .generated_text;
    if matches!(format, ActionPromptFormat::Gemini) {
        serde_json::from_str::<String>(&result).context("Invalid Gemini text response")
    } else {
        Ok(result)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn action_parameters_select_provider_and_preserve_explanation_token_cap() {
        let options: Kwargs = serde_json::from_value(
            json!({"fim":{},"max_tokens":900,"generationConfig":{"maxOutputTokens":500}}),
        )
        .unwrap();
        for format in [
            ActionPromptFormat::Messages,
            ActionPromptFormat::Anthropic,
            ActionPromptFormat::Gemini,
        ] {
            let params = parameters(&options, format, "Instruction", 2048, Some(128)).unwrap();
            assert!(params.get("fim").is_none());
            assert_eq!(params["max_tokens"], 128);
            assert_eq!(params["generationConfig"]["maxOutputTokens"], 128);
            match format {
                ActionPromptFormat::Messages => {
                    assert_eq!(params["messages"][0]["content"], "Instruction")
                }
                ActionPromptFormat::Anthropic => assert_eq!(params["system"], "Instruction"),
                ActionPromptFormat::Gemini => assert_eq!(
                    params["systemInstruction"]["parts"][0]["text"],
                    "Instruction"
                ),
            }
        }
        assert_eq!(options["max_tokens"], 900);
    }
}
