//! Shared, revisioned document snapshots for dynamic code actions.
use crate::code_action_edits::position_to_char;
use lsp_server::{Connection, Message, Notification};
use lsp_types::{
    Diagnostic, DidChangeTextDocumentParams, DidOpenTextDocumentParams, PublishDiagnosticsParams,
    Url,
};
use parking_lot::Mutex;
use ropey::Rope;
use std::{
    collections::HashMap,
    sync::atomic::{AtomicU64, Ordering},
};

#[derive(Debug)]
pub(crate) struct ContentModified;
impl std::fmt::Display for ContentModified {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("Document changed; request the code action again")
    }
}
impl std::error::Error for ContentModified {}

#[derive(Clone)]
pub(crate) struct Document {
    pub(crate) text: Rope,
    pub(crate) language_id: String,
    pub(crate) version: i32,
    pub(crate) revision: u64,
    // Each entry contains the original diagnostic and its explanation.
    pub(crate) explanations: Vec<(Diagnostic, Diagnostic)>,
}

#[derive(Default)]
pub(crate) struct DocumentStore {
    pub(crate) documents: Mutex<HashMap<Url, Document>>,
    revision: AtomicU64,
}

impl DocumentStore {
    fn next_revision(&self) -> u64 {
        self.revision.fetch_add(1, Ordering::Relaxed) + 1
    }

    pub(crate) fn opened(
        &self,
        params: &DidOpenTextDocumentParams,
        connection: &Connection,
    ) -> anyhow::Result<()> {
        let item = &params.text_document;
        let mut documents = self.documents.lock();
        if documents
            .get(&item.uri)
            .is_some_and(|doc| !doc.explanations.is_empty())
        {
            publish(connection, item.uri.clone(), Some(item.version), vec![])?;
        }
        documents.insert(
            item.uri.clone(),
            Document {
                text: Rope::from_str(&item.text),
                language_id: item.language_id.clone(),
                version: item.version,
                revision: self.next_revision(),
                explanations: vec![],
            },
        );
        Ok(())
    }

    pub(crate) fn changed(
        &self,
        params: &DidChangeTextDocumentParams,
        connection: &Connection,
    ) -> anyhow::Result<()> {
        let mut documents = self.documents.lock();
        let Some(doc) = documents.get_mut(&params.text_document.uri) else {
            return Ok(());
        };
        let mut text = doc.text.clone();
        for change in &params.content_changes {
            if let Some(range) = change.range {
                let start = position_to_char(&text, range.start)?;
                let end = position_to_char(&text, range.end)?;
                anyhow::ensure!(start <= end, "Invalid change range");
                text.remove(start..end);
                text.insert(start, &change.text);
            } else {
                text = Rope::from_str(&change.text);
            }
        }
        doc.text = text;
        doc.version = params.text_document.version;
        doc.revision = self.next_revision();
        if !doc.explanations.is_empty() {
            doc.explanations.clear();
            publish(
                connection,
                params.text_document.uri.clone(),
                Some(doc.version),
                vec![],
            )?;
        }
        Ok(())
    }

    pub(crate) fn closed(&self, uri: &Url, connection: &Connection) -> anyhow::Result<()> {
        if self
            .documents
            .lock()
            .remove(uri)
            .is_some_and(|doc| !doc.explanations.is_empty())
        {
            publish(connection, uri.clone(), None, vec![])?;
        }
        Ok(())
    }

    pub(crate) fn renamed(
        &self,
        old: &Url,
        new: Url,
        connection: &Connection,
    ) -> anyhow::Result<()> {
        let mut documents = self.documents.lock();
        if let Some(mut doc) = documents.remove(old) {
            if !doc.explanations.is_empty() {
                publish(connection, old.clone(), None, vec![])?;
            }
            doc.explanations.clear();
            doc.revision = self.next_revision();
            documents.insert(new, doc);
        }
        Ok(())
    }

    pub(crate) fn snapshot(&self, uri: &Url, revision: u64) -> anyhow::Result<Document> {
        self.documents
            .lock()
            .get(uri)
            .filter(|doc| doc.revision == revision)
            .cloned()
            .ok_or_else(|| ContentModified.into())
    }
}

pub(crate) fn publish(
    connection: &Connection,
    uri: Url,
    version: Option<i32>,
    diagnostics: Vec<Diagnostic>,
) -> anyhow::Result<()> {
    connection
        .sender
        .send(Message::Notification(Notification::new(
            "textDocument/publishDiagnostics".into(),
            PublishDiagnosticsParams {
                uri,
                diagnostics,
                version,
            },
        )))?;
    Ok(())
}
