//! UTF-16 positions and validated, unambiguous same-file replacements.
use anyhow::{bail, Context};
use lsp_types::{Position, Range, TextEdit};
use ropey::Rope;
use serde::Deserialize;

// The server advertises no alternative position encoding, so LSP positions are UTF-16.
pub(crate) fn position_to_char(text: &Rope, position: Position) -> anyhow::Result<usize> {
    let line = text
        .get_line(position.line as usize)
        .context("Position line is out of bounds")?;
    let mut units = 0;
    let mut chars = 0;
    for ch in line.chars() {
        if units == position.character as usize {
            break;
        }
        anyhow::ensure!(ch != '\n' && ch != '\r', "Position is past end of line");
        units += ch.len_utf16();
        chars += 1;
    }
    anyhow::ensure!(
        units == position.character as usize,
        "Invalid UTF-16 character position"
    );
    Ok(text.line_to_char(position.line as usize) + chars)
}

pub(crate) fn char_to_position(text: &Rope, offset: usize) -> Position {
    let line = text.char_to_line(offset);
    let start = text.line_to_char(line);
    let character = text
        .slice(start..offset)
        .chars()
        .map(char::len_utf16)
        .sum::<usize>();
    Position::new(line as u32, character as u32)
}

pub(crate) fn context_code(
    text: &Rope,
    position: Position,
    max_chars: usize,
) -> anyhow::Result<String> {
    let anchor = position_to_char(text, position)?;
    let start = anchor.saturating_sub(max_chars / 2);
    let end = (start + max_chars).min(text.len_chars());
    let start = end.saturating_sub(max_chars);
    Ok(text.slice(start..end).to_string())
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Fix {
    edits: Vec<Replacement>,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Replacement {
    old_text: String,
    new_text: String,
}

pub(crate) fn fix_edits(
    text: &Rope,
    visible_code: &str,
    response: &str,
) -> anyhow::Result<Vec<TextEdit>> {
    let response = response.trim();
    let response = if let Some(fenced) = response
        .strip_prefix("```json")
        .or_else(|| response.strip_prefix("```"))
    {
        fenced
            .trim()
            .strip_suffix("```")
            .context("Unclosed JSON fence")?
            .trim()
    } else {
        response
    };
    let fix: Fix =
        serde_json::from_str(response).context("Model did not return valid edit JSON")?;
    anyhow::ensure!(!fix.edits.is_empty(), "Model returned no edits");
    let source = text.to_string();
    let mut offsets = vec![];
    for replacement in fix.edits {
        anyhow::ensure!(
            !replacement.old_text.is_empty(),
            "Empty search text is not allowed"
        );
        anyhow::ensure!(
            visible_code.contains(&replacement.old_text),
            "Edit is outside the supplied context"
        );
        let Some(start) = source.find(&replacement.old_text) else {
            bail!("Edit does not match the current code")
        };
        // Include overlapping matches, e.g. 'aa' inside 'aaa'.
        let after_first_char = start + source[start..].chars().next().unwrap().len_utf8();
        anyhow::ensure!(
            !source[after_first_char..].contains(&replacement.old_text),
            "Edit search text is ambiguous"
        );
        let end = start + replacement.old_text.len();
        anyhow::ensure!(
            replacement.old_text != replacement.new_text,
            "Model returned an unchanged edit"
        );
        offsets.push((start, end, replacement.new_text));
    }
    offsets.sort_by_key(|(start, _, _)| *start);
    for pair in offsets.windows(2) {
        anyhow::ensure!(pair[0].1 <= pair[1].0, "Model returned overlapping edits");
    }
    Ok(offsets
        .into_iter()
        .map(|(start, end, new_text)| TextEdit {
            range: Range::new(
                char_to_position(text, text.byte_to_char(start)),
                char_to_position(text, text.byte_to_char(end)),
            ),
            new_text,
        })
        .collect())
}
