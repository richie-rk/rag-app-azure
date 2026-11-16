// Every user-visible string of the web client (10-web-client.md section 12.6), under the keys of
// that file's text tables and of its access, shell and error-screen tables. `CopyTexts` names each
// key, so a missing or an extra key fails `tsc`. Components read text only from here, insert
// `{name}` values with `fillText` and pick plural forms with `pluralText` (`src/ui/format.ts`), and
// always render the result as text.

import type {
  AdminProjectSummary,
  ContentType,
  DocumentSummary,
  FactCategory,
  FeedbackCategory,
  IndexSummary,
  RunItem,
  RunSummary,
  VersionSummary,
} from "../api/generated/types.gen";

/** Plural forms, chosen with `Intl.PluralRules`; `zero` only where the table lists it. */
export interface PluralText {
  readonly zero?: string;
  readonly one: string;
  readonly other: string;
}

/** A filter's label and the text of each of its options, keyed by the option's URL value. */
export interface FilterText {
  readonly label: string;
  readonly options: Readonly<Record<string, string>>;
}

/** `chat.heading`: the heading of a new chat, and of an open session. */
export interface HeadingText {
  readonly new: string;
  readonly session: string;
}

export interface CopyTexts {
  readonly "app.starting": string;
  readonly "app.redirecting": string;
  readonly "access.unsupported.title": string;
  readonly "access.unsupported.body": string;
  readonly "app.configError.title": string;
  readonly "app.configError.body": string;
  readonly "access.signedOut.title": string;
  readonly "access.signedOut.body": string;
  readonly "access.signedOut.action": string;
  readonly "access.notAssigned.title": string;
  readonly "access.notAssigned.body": string;
  readonly "access.consentMissing.title": string;
  readonly "access.consentMissing.body": string;
  readonly "access.signInFailed.title": string;
  readonly "access.signInFailed.body": string;
  readonly "access.loadUserFailed.title": string;
  readonly "access.loadUserFailed.body": string;
  readonly "access.noProjects.title": string;
  readonly "access.noProjects.body": string;
  readonly "access.noProjects.action": string;
  readonly "access.noProjectsAdmin.title": string;
  readonly "access.noProjectsAdmin.body": string;
  readonly "access.noProjectsAdmin.action": string;
  readonly "access.adminOnly.title": string;
  readonly "access.adminOnly.body": string;
  readonly "access.adminOnly.action": string;
  readonly "session.expired.title": string;
  readonly "session.expired.body": string;
  readonly "session.expired.action": string;
  readonly "shell.signOut.title": string;
  readonly "shell.signOut.body": string;
  readonly "shell.signOut.confirm": string;
  readonly "shell.signOut.cancel": string;
  readonly "shell.skipLink": string;
  readonly "shell.nav.show": string;
  readonly "shell.nav.hide": string;
  readonly "shell.productName": string;
  readonly "shell.theme": string;
  readonly "shell.account.label": string;
  readonly "shell.account.signOut": string;
  readonly "shell.nav.label": string;
  readonly "shell.nav.admin": string;
  readonly "shell.nav.projects": string;
  readonly "shell.nav.indexes": string;
  readonly "shell.nav.runs": string;
  readonly "shell.nav.chat": string;
  readonly "shell.nav.memory": string;
  readonly "shell.nav.recent": string;
  readonly "shell.nav.newChat": string;
  readonly "shell.nav.noChats": string;
  readonly "shell.nav.recentError": string;
  readonly "app.updated.title": string;
  readonly "app.updated.body": string;
  readonly "app.crashed.title": string;
  readonly "app.crashed.body": string;
  readonly "admin.showMore": string;
  readonly "admin.actionsFor": string;
  readonly "admin.backToList": string;
  readonly "admin.notSet": string;
  readonly "confirm.cancel": string;
  readonly "memory.title": string;
  readonly "memory.intro": string;
  readonly "memory.toggle": string;
  readonly "memory.disabledBar": string;
  readonly "memory.pausedBar": string;
  readonly "memory.empty": string;
  readonly "memory.list": string;
  readonly "memory.updated": string;
  readonly "memory.edit": string;
  readonly "memory.editName": string;
  readonly "memory.delete": string;
  readonly "memory.deleteName": string;
  readonly "memory.editLabel": string;
  readonly "memory.field.category": string;
  readonly "memory.save": string;
  readonly "memory.cancel": string;
  readonly "memory.reason.empty": string;
  readonly "memory.reason.too_long": string;
  readonly "memory.reason.contains_link": string;
  readonly "memory.reason.contains_code": string;
  readonly "memory.reason.contains_instruction": string;
  readonly "memory.reason.contains_secret": string;
  readonly "memory.reason.duplicate_fact": string;
  readonly "memory.reason.memory_full": string;
  readonly "memory.usage.facts": PluralText;
  readonly "memory.usage.space": string;
  readonly "memory.usage.spaceValue": string;
  readonly "memory.deleteConfirm.title": string;
  readonly "memory.deleteConfirm.body": string;
  readonly "memory.deleteConfirm.action": string;
  readonly "memory.clearAll": string;
  readonly "memory.clearConfirm.title": string;
  readonly "memory.clearConfirm.body": string;
  readonly "memory.clearConfirm.action": string;
  readonly "memory.conflict": string;
  readonly "memory.factGone": string;
  readonly "memory.saveUnavailable": string;
  readonly "memory.saved": string;
  readonly "memory.deleted": string;
  readonly "memory.cleared": string;
  readonly "memory.pausedDone": string;
  readonly "memory.resumedDone": string;
  readonly "memory.loadError": string;
  readonly "admin.conflict.body": string;
  readonly "admin.conflict.latestValue": string;
  readonly "admin.conflict.useLatest": string;
  readonly "admin.conflict.action": string;
  readonly "projects.title": string;
  readonly "projects.new": string;
  readonly "projects.filter": string;
  readonly "projects.statusFilter": FilterText;
  readonly "projects.col.name": string;
  readonly "projects.col.defaultIndex": string;
  readonly "projects.col.members": string;
  readonly "projects.col.newUsersJoin": string;
  readonly "projects.col.status": string;
  readonly "projects.yes": string;
  readonly "projects.no": string;
  readonly "projects.noIndex": string;
  readonly "projects.empty": string;
  readonly "projects.noMatches": string;
  readonly "projects.loadError": string;
  readonly "project.new.title": string;
  readonly "project.form.members": string;
  readonly "project.form.members.hint": string;
  readonly "project.form.members.notFound": string;
  readonly "project.form.name": string;
  readonly "project.form.description": string;
  readonly "project.form.description.hint": string;
  readonly "project.form.examples": string;
  readonly "project.form.examples.hint": string;
  readonly "project.form.examples.add": string;
  readonly "project.form.examples.item": string;
  readonly "project.form.examples.remove": string;
  readonly "project.form.instructions": string;
  readonly "project.form.instructions.hint": string;
  readonly "project.form.joinNewUsers": string;
  readonly "project.form.joinNewUsers.hint": string;
  readonly "project.form.required": string;
  readonly "project.form.tooLong": string;
  readonly "project.create": string;
  readonly "project.save": string;
  readonly "project.cancel": string;
  readonly "project.created": string;
  readonly "project.savedToast": string;
  readonly "project.tabs.details": string;
  readonly "project.tabs.members": string;
  readonly "project.tabs.indexes": string;
  readonly "project.archive": string;
  readonly "project.archiveConfirm.title": string;
  readonly "project.archiveConfirm.body": string;
  readonly "project.archiveConfirm.action": string;
  readonly "project.archived": string;
  readonly "project.restore": string;
  readonly "project.restored": string;
  readonly "members.add": string;
  readonly "members.picker.label": string;
  readonly "members.picker.placeholder": string;
  readonly "members.picker.minChars": string;
  readonly "members.picker.none": string;
  readonly "members.addAction": string;
  readonly "members.added": PluralText;
  readonly "members.addFailed": string;
  readonly "members.waiting": string;
  readonly "members.col.name": string;
  readonly "members.col.email": string;
  readonly "members.col.addedBy": string;
  readonly "members.col.added": string;
  readonly "members.addedByDefault": string;
  readonly "members.addedByAdmin": string;
  readonly "members.remove": string;
  readonly "members.removeName": string;
  readonly "members.removeConfirm.title": string;
  readonly "members.removeConfirm.body": string;
  readonly "members.removeConfirm.action": string;
  readonly "members.removed": string;
  readonly "members.empty": string;
  readonly "projectIndexes.map": string;
  readonly "projectIndexes.picker": string;
  readonly "projectIndexes.makeDefaultSwitch": string;
  readonly "projectIndexes.mapAction": string;
  readonly "projectIndexes.col.index": string;
  readonly "projectIndexes.col.default": string;
  readonly "projectIndexes.col.status": string;
  readonly "projectIndexes.defaultBadge": string;
  readonly "projectIndexes.makeDefault": string;
  readonly "projectIndexes.unmap": string;
  readonly "projectIndexes.unmapConfirm.title": string;
  readonly "projectIndexes.unmapConfirm.body": string;
  readonly "projectIndexes.unmapConfirm.action": string;
  readonly "projectIndexes.empty": string;
  readonly "projectIndexes.noneAvailable": string;
  readonly "projectIndexes.mapped": string;
  readonly "projectIndexes.defaultSet": string;
  readonly "projectIndexes.unmapped": string;
  readonly "admin.autoRefresh": string;
  readonly "admin.poll.stale": string;
  readonly "admin.poll.stopped": string;
  readonly "admin.poll.refresh": string;
  readonly "indexes.title": string;
  readonly "indexes.new": string;
  readonly "indexes.col.name": string;
  readonly "indexes.col.method": string;
  readonly "indexes.col.embeddingModel": string;
  readonly "indexes.col.documents": string;
  readonly "indexes.col.status": string;
  readonly "indexes.embeddingValue": string;
  readonly "indexes.empty": string;
  readonly "indexes.loadError": string;
  readonly "index.new.title": string;
  readonly "index.form.name": string;
  readonly "index.form.description": string;
  readonly "index.form.method": string;
  readonly "index.form.method.hint": string;
  readonly "index.form.params": string;
  readonly "index.form.paramsUnsupported": string;
  readonly "index.form.embedding": string;
  readonly "index.form.dimensions": string;
  readonly "index.form.dimensions.hint": string;
  readonly "index.form.language": string;
  readonly "index.form.language.hint": string;
  readonly "index.create": string;
  readonly "index.created": string;
  readonly "index.delete": string;
  readonly "index.deleteConfirm.title": string;
  readonly "index.deleteConfirm.body": string;
  readonly "index.deleteConfirm.action": string;
  readonly "index.deleted": string;
  readonly "index.optionsError": string;
  readonly "index.edit": string;
  readonly "index.save": string;
  readonly "index.saved": string;
  readonly "index.changeWarning": string;
  readonly "index.reingestBanner.title": string;
  readonly "index.reingestBanner.body": string;
  readonly "index.reingest": string;
  readonly "index.reingestConfirm.title": string;
  readonly "index.reingestConfirm.body": string;
  readonly "index.reingestConfirm.action": string;
  readonly "index.reingestStarted": string;
  readonly "index.viewProgress": string;
  readonly "index.documents": string;
  readonly "index.runs": string;
  readonly "index.rebuildProgress": string;
  readonly "index.summary.method": string;
  readonly "index.summary.settings": string;
  readonly "index.summary.embeddingModel": string;
  readonly "index.summary.language": string;
  readonly "index.summary.status": string;
  readonly "index.summary.documents": string;
  readonly "documents.title": string;
  readonly "documents.project": string;
  readonly "documents.project.hint": string;
  readonly "documents.project.none": string;
  readonly "documents.project.noneLink": string;
  readonly "documents.status": string;
  readonly "documents.upload": string;
  readonly "documents.filter": string;
  readonly "documents.stateFilter": FilterText;
  readonly "documents.col.name": string;
  readonly "documents.col.version": string;
  readonly "documents.col.state": string;
  readonly "documents.col.size": string;
  readonly "documents.col.updated": string;
  readonly "documents.reingest": string;
  readonly "documents.reingestStarted": string;
  readonly "documents.delete": string;
  readonly "documents.deleteConfirm.title": string;
  readonly "documents.deleteConfirm.body": string;
  readonly "documents.deleteConfirm.action": string;
  readonly "documents.deleted": string;
  readonly "documents.empty": string;
  readonly "documents.noMatches": string;
  readonly "documentPanel.versions": string;
  readonly "documentPanel.col.version": string;
  readonly "documentPanel.col.state": string;
  readonly "documentPanel.col.uploaded": string;
  readonly "documentPanel.col.uploadedBy": string;
  readonly "documentPanel.col.throughProject": string;
  readonly "documentPanel.col.size": string;
  readonly "documentPanel.col.scanResult": string;
  readonly "documentPanel.col.problem": string;
  readonly "documentPanel.close": string;
  readonly "documentPanel.downloadPdf": string;
  readonly "documentPanel.pdfTooLarge": string;
  readonly "runs.title": string;
  readonly "runs.filter.index": string;
  readonly "runs.filter.indexAll": string;
  readonly "runs.filter.state": string;
  readonly "runs.filter.stateAll": string;
  readonly "runs.col.started": string;
  readonly "runs.col.type": string;
  readonly "runs.col.index": string;
  readonly "runs.col.startedBy": string;
  readonly "runs.col.progress": string;
  readonly "runs.col.state": string;
  readonly "runs.progress": string;
  readonly "runs.progressFailed": string;
  readonly "runs.empty": string;
  readonly "runs.noMatches": string;
  readonly "runs.announce.finished": string;
  readonly "run.title": string;
  readonly "run.field.state": string;
  readonly "run.field.type": string;
  readonly "run.field.index": string;
  readonly "run.field.startedBy": string;
  readonly "run.field.started": string;
  readonly "run.field.finished": string;
  readonly "run.notFinished": string;
  readonly "run.system": string;
  readonly "run.items": string;
  readonly "run.col.document": string;
  readonly "run.col.version": string;
  readonly "run.col.stage": string;
  readonly "run.col.state": string;
  readonly "run.col.attempts": string;
  readonly "run.col.problem": string;
  readonly "run.problem": string;
  readonly "chat.heading": HeadingText;
  readonly "chat.sessions.label": string;
  readonly "chat.sessions.open": string;
  readonly "chat.sessions.new": string;
  readonly "chat.sessions.empty": string;
  readonly "chat.sessions.more": string;
  readonly "chat.sessions.error": string;
  readonly "chat.sessions.answering": string;
  readonly "chat.project.label": string;
  readonly "chat.project.placeholder": string;
  readonly "chat.project.chooseFirst": string;
  readonly "chat.project.changeHint": string;
  readonly "chat.project.notReadySuffix": string;
  readonly "chat.project.notReady": string;
  readonly "chat.projects.error": string;
  readonly "chat.empty.title": string;
  readonly "chat.empty.body": string;
  readonly "chat.empty.examples": string;
  readonly "chat.composer.label": string;
  readonly "chat.composer.placeholder": string;
  readonly "chat.composer.hint": string;
  readonly "chat.composer.counter": string;
  readonly "chat.composer.tooLong": string;
  readonly "chat.composer.reason.empty": string;
  readonly "chat.composer.reason.invalid_characters": string;
  readonly "chat.composer.send": string;
  readonly "chat.composer.stop": string;
  readonly "chat.composer.blockedUntil": string;
  readonly "chat.conversation.label": string;
  readonly "chat.turn.youAsked": string;
  readonly "chat.turn.answerLabel": string;
  readonly "chat.turn.searching": string;
  readonly "chat.turn.broadened": string;
  readonly "chat.turn.sources": string;
  readonly "chat.turn.citation": string;
  readonly "chat.locator.page": string;
  readonly "chat.turn.linkSuffix": string;
  readonly "chat.turn.linkRemoved": string;
  readonly "chat.turn.table": string;
  readonly "chat.turn.helpful": string;
  readonly "chat.turn.notHelpful": string;
  readonly "chat.turn.copy": string;
  readonly "chat.turn.copied": string;
  readonly "chat.turn.copyFailed": string;
  readonly "chat.turn.stopped": string;
  readonly "chat.turn.stoppedStored": string;
  readonly "chat.turn.truncated": string;
  readonly "chat.turn.failedStored": string;
  readonly "chat.turn.inProgress": string;
  readonly "chat.turn.inProgressLong": string;
  readonly "chat.turn.refresh": string;
  readonly "chat.turn.autoRefresh": string;
  readonly "chat.turn.retry": string;
  readonly "chat.followups.label": string;
  readonly "chat.earlier.load": string;
  readonly "chat.earlier.error": string;
  readonly "chat.jumpToLatest": string;
  readonly "chat.unavailable.action": string;
  readonly "chat.error.title": string;
  readonly "chat.error.cutOff": string;
  readonly "chat.error.stalled": string;
  readonly "chat.error.unreadable": string;
  readonly "chat.error.network": string;
  readonly "chat.announce.done": PluralText;
  readonly "chat.announce.stopped": string;
  readonly "chat.announce.failed": string;
  readonly "citation.title": string;
  readonly "citation.close": string;
  readonly "citation.loading": string;
  readonly "citation.frame": string;
  readonly "citation.type.pdf": string;
  readonly "citation.type.docx": string;
  readonly "citation.type.pptx": string;
  readonly "citation.field.type": string;
  readonly "citation.field.location": string;
  readonly "citation.field.file": string;
  readonly "citation.noPdfViewer": string;
  readonly "citation.downloadPage": string;
  readonly "citation.downloadDocument": string;
  readonly "citation.documentTooLarge": string;
  readonly "citation.downloading": string;
  readonly "citation.downloadFailed": string;
  readonly "citation.error": string;
  readonly "feedback.title": string;
  readonly "feedback.titleUp": string;
  readonly "feedback.addComment": string;
  readonly "feedback.editComment": string;
  readonly "feedback.category.label": string;
  readonly "feedback.category.incorrect": string;
  readonly "feedback.category.incomplete": string;
  readonly "feedback.category.notRelevant": string;
  readonly "feedback.category.other": string;
  readonly "feedback.comment.label": string;
  readonly "feedback.comment.hint": string;
  readonly "feedback.comment.counter": string;
  readonly "feedback.submit": string;
  readonly "feedback.sending": string;
  readonly "feedback.cancel": string;
  readonly "feedback.saved": string;
  readonly "feedback.reason.category_requires_down": string;
  readonly "feedback.reason.too_long": string;
  readonly "feedback.reason.comments_disabled": string;
  readonly "upload.title": string;
  readonly "upload.choose": string;
  readonly "upload.drop": string;
  readonly "upload.checking": string;
  readonly "upload.tooMany": string;
  readonly "upload.col.file": string;
  readonly "upload.col.result": string;
  readonly "upload.result.new": string;
  readonly "upload.result.unchanged": string;
  readonly "upload.result.duplicate": string;
  readonly "upload.result.update": string;
  readonly "upload.result.updateLabel": string;
  readonly "upload.result.rejectedType": string;
  readonly "upload.result.rejectedSize": string;
  readonly "upload.result.rejected": string;
  readonly "upload.result.unreadable": string;
  readonly "upload.result.empty": string;
  readonly "upload.result.invalidName": string;
  readonly "upload.result.duplicateInBatch": string;
  readonly "upload.nothingToUpload": string;
  readonly "upload.sharedIndex": PluralText;
  readonly "upload.sharedIndex.more": string;
  readonly "upload.sharedIndex.unknown": string;
  readonly "upload.start": PluralText;
  readonly "upload.progress": string;
  readonly "upload.fileDone": string;
  readonly "upload.fileFailed": string;
  readonly "upload.fileStopped": string;
  readonly "upload.stalled": string;
  readonly "upload.stop": string;
  readonly "upload.filePaused": string;
  readonly "upload.complete.title": string;
  readonly "upload.complete.body": PluralText;
  readonly "upload.partial": string;
  readonly "upload.viewProgress": string;
  readonly "upload.close": string;
  readonly "error.reference": string;
  readonly "error.copyReference": string;
  readonly "error.referenceCopied": string;
  readonly "error.retry": string;
  readonly "error.retryAt": string;
  readonly "error.network.title": string;
  readonly "error.network.body": string;
  readonly "error.server.title": string;
  readonly "error.server.body": string;
  readonly "error.unexpected.title": string;
  readonly "error.unexpected.body": string;
  readonly "error.validation.field": string;
  readonly "error.sessionExpired": string;
  readonly "error.defect.body": string;
  readonly "app.reload": string;
  readonly "access.retry": string;
  readonly "access.signOut": string;
}

export const en: CopyTexts = {
  "app.starting": "Starting DocChat…",
  "app.redirecting": "Taking you to sign-in…",
  "access.unsupported.title": "This browser can't run DocChat",
  "access.unsupported.body":
    "Open DocChat in a current version of Microsoft Edge, Google Chrome, Mozilla Firefox or Apple Safari.",
  "app.configError.title": "DocChat can't start",
  "app.configError.body":
    "Its settings didn't load. Reload the page. If this keeps happening, tell your DocChat administrator.",
  "access.signedOut.title": "You're signed out",
  "access.signedOut.body": "Sign in with your work account to use DocChat.",
  "access.signedOut.action": "Sign in",
  "access.notAssigned.title": "You don't have access to DocChat yet",
  "access.notAssigned.body":
    "Your account, {account}, isn't set up to use DocChat. Ask your DocChat administrator to give you access, then select Retry.",
  "access.consentMissing.title": "DocChat isn't set up for your organization yet",
  "access.consentMissing.body":
    "An administrator has to finish setting up DocChat before anyone can sign in. Send them the reference ID below.",
  "access.signInFailed.title": "Sign-in didn't finish",
  "access.signInFailed.body":
    "Select Retry. If sign-in keeps failing, send the reference ID below to your DocChat administrator.",
  "access.loadUserFailed.title": "We couldn't load your account",
  "access.loadUserFailed.body":
    "Select Retry. If this keeps happening, send the reference ID below to your DocChat administrator.",
  "access.noProjects.title": "You aren't in any projects yet",
  "access.noProjects.body":
    "Ask your DocChat administrator to add you to a project. Your projects appear here as soon as you're added.",
  "access.noProjects.action": "Refresh",
  "access.noProjectsAdmin.title": "There are no projects yet",
  "access.noProjectsAdmin.body":
    "Create a project and map an index to it, then come back here to chat with its documents.",
  "access.noProjectsAdmin.action": "Go to Projects",
  "access.adminOnly.title": "This page is for administrators",
  "access.adminOnly.body":
    "You need the DocChat administrator role to open it. If you think you should have it, ask your DocChat administrator.",
  "access.adminOnly.action": "Go to chat",
  "session.expired.title": "Your sign-in has expired",
  "session.expired.body":
    "Sign in to keep working. Copy any text you haven't sent first, because signing in reloads the page.",
  "session.expired.action": "Sign in",
  "shell.signOut.title": "Sign out now?",
  "shell.signOut.body": "An answer or upload is still in progress. Signing out stops it.",
  "shell.signOut.confirm": "Sign out",
  "shell.signOut.cancel": "Stay signed in",
  "shell.skipLink": "Skip to main content",
  "shell.nav.show": "Show navigation",
  "shell.nav.hide": "Hide navigation",
  "shell.productName": "DocChat",
  "shell.theme": "Dark theme",
  "shell.account.label": "Account: {displayName}",
  "shell.account.signOut": "Sign out",
  "shell.nav.label": "Main navigation",
  "shell.nav.admin": "Administration",
  "shell.nav.projects": "Projects",
  "shell.nav.indexes": "Indexes",
  "shell.nav.runs": "Ingestion runs",
  "shell.nav.chat": "Chat",
  "shell.nav.memory": "Memory",
  "shell.nav.recent": "Recent chats",
  "shell.nav.newChat": "New chat",
  "shell.nav.noChats": "No chats yet.",
  "shell.nav.recentError": "Recent chats didn't load.",
  "app.updated.title": "DocChat has been updated",
  "app.updated.body": "Reload the page to get the latest version.",
  "app.crashed.title": "Something went wrong on this page",
  "app.crashed.body":
    "Reload the page. If this keeps happening, tell your DocChat administrator what you were doing.",
  "admin.showMore": "Show more",
  "admin.actionsFor": "Actions for {name}",
  "admin.backToList": "Back to {list}",
  "admin.notSet": "Not set",
  "confirm.cancel": "Cancel",
  "memory.title": "Memory",
  "memory.intro":
    "DocChat remembers lasting details you mention in chats, such as your role or how you like answers written, and uses them in later chats. It never saves anything from your documents.",
  "memory.toggle": "Use memory",
  "memory.disabledBar":
    "Memory is turned off for everyone by your DocChat administrator. Your saved details stay here, and you can still edit or delete them.",
  "memory.pausedBar": "Memory is paused. DocChat isn't saving new details or using saved ones.",
  "memory.empty": "Nothing saved yet. Details you share in chats will appear here.",
  "memory.list": "Saved details",
  "memory.updated": "Updated {date}",
  "memory.edit": "Edit",
  "memory.editName": "Edit memory: {fact}",
  "memory.delete": "Delete",
  "memory.deleteName": "Delete memory: {fact}",
  "memory.editLabel": "Memory",
  "memory.field.category": "Category",
  "memory.save": "Save",
  "memory.cancel": "Cancel",
  "memory.reason.empty": "Enter some text for this memory.",
  "memory.reason.too_long": "A memory can be up to {max} characters. Shorten it, then save.",
  "memory.reason.contains_link":
    "Memories can't include web addresses. Remove the link, then save.",
  "memory.reason.contains_code": "Memories can't include code. Describe it in plain words instead.",
  "memory.reason.contains_instruction":
    "Write this as a fact about you, such as your role or how you like answers, rather than as an instruction.",
  "memory.reason.contains_secret":
    "This looks like a password or other secret. Don't keep secrets in memory.",
  "memory.reason.duplicate_fact": "You already have a memory that says this.",
  "memory.reason.memory_full":
    "Your memory is full. Delete or shorten other memories, then save this one.",
  "memory.usage.facts": {
    one: "1 of {max} memories saved",
    other: "{count} of {max} memories saved",
  },
  "memory.usage.space": "Memory space",
  "memory.usage.spaceValue": "{percent}% of memory space used",
  "memory.deleteConfirm.title": "Delete this memory?",
  "memory.deleteConfirm.body": "DocChat stops using it in answers. You can't undo this.",
  "memory.deleteConfirm.action": "Delete",
  "memory.clearAll": "Clear all",
  "memory.clearConfirm.title": "Clear all memories?",
  "memory.clearConfirm.body":
    "DocChat deletes everything it has saved about you. You can't undo this.",
  "memory.clearConfirm.action": "Clear all",
  "memory.conflict":
    "The latest version is shown now. Make your change once more if you still need it.",
  "memory.factGone": "The list is up to date now.",
  "memory.saveUnavailable": "Your text is still here. Select Save to retry.",
  "memory.saved": "Memory updated.",
  "memory.deleted": "Memory deleted.",
  "memory.cleared": "All memories cleared.",
  "memory.pausedDone": "Memory paused.",
  "memory.resumedDone": "Memory turned on.",
  "memory.loadError": "Your memory didn't load.",
  "admin.conflict.body":
    "Your edits are still here. Fields that changed show the latest saved value. Review them, then save to replace them.",
  "admin.conflict.latestValue": "Latest saved value: {value}",
  "admin.conflict.useLatest": "Use latest value",
  "admin.conflict.action":
    "The latest version is shown now. Check it, then repeat the action if you still need it.",
  "projects.title": "Projects",
  "projects.new": "New project",
  "projects.filter": "Filter by name",
  "projects.statusFilter": {
    label: "Status",
    options: {
      active: "Active",
      archived: "Archived",
      all: "All",
    },
  },
  "projects.col.name": "Name",
  "projects.col.defaultIndex": "Default index",
  "projects.col.members": "Members",
  "projects.col.newUsersJoin": "New users join",
  "projects.col.status": "Status",
  "projects.yes": "Yes",
  "projects.no": "No",
  "projects.noIndex": "None",
  "projects.empty": "No projects yet. Create one to get started.",
  "projects.noMatches": "No projects match these filters.",
  "projects.loadError": "Projects didn't load.",
  "project.new.title": "New project",
  "project.form.members": "Members",
  "project.form.members.hint":
    "Search by name or email. People appear here after they first sign in to DocChat.",
  "project.form.members.notFound":
    "{name} doesn't have a DocChat account, so they can't be added. Remove them, then create the project.",
  "project.form.name": "Name",
  "project.form.description": "Description",
  "project.form.description.hint": "Shown to people when they start a chat in this project.",
  "project.form.examples": "Example questions",
  "project.form.examples.hint": "Shown as suggestions when a chat starts. Add up to {max}.",
  "project.form.examples.add": "Add example question",
  "project.form.examples.item": "Example question {n}",
  "project.form.examples.remove": "Remove example question {n}",
  "project.form.instructions": "Instructions for answers",
  "project.form.instructions.hint":
    "Plain text added to every question asked in this project, such as the tone to use or terms to explain. It can't override DocChat's own rules.",
  "project.form.joinNewUsers": "Add new users automatically",
  "project.form.joinNewUsers.hint":
    "People who sign in to DocChat for the first time join this project. Turning this on doesn't add people who have already signed in.",
  "project.form.required": "Enter a name.",
  "project.form.tooLong": "This can be up to {max} characters.",
  "project.create": "Create project",
  "project.save": "Save changes",
  "project.cancel": "Cancel",
  "project.created": "Project created.",
  "project.savedToast": "Changes saved.",
  "project.tabs.details": "Details",
  "project.tabs.members": "Members",
  "project.tabs.indexes": "Indexes",
  "project.archive": "Archive project",
  "project.archiveConfirm.title": "Archive {name}?",
  "project.archiveConfirm.body":
    "People can't chat in an archived project, and its chats are hidden until you restore it. Members and index mappings are kept.",
  "project.archiveConfirm.action": "Archive",
  "project.archived": "Project archived.",
  "project.restore": "Restore project",
  "project.restored": "Project restored.",
  "members.add": "Add people",
  "members.picker.label": "People to add",
  "members.picker.placeholder": "Search by name or email",
  "members.picker.minChars": "Type at least 2 characters to search.",
  "members.picker.none": "No one found. People appear here after they first sign in to DocChat.",
  "members.addAction": "Add",
  "members.added": {
    one: "1 person added.",
    other: "{count} people added.",
  },
  "members.addFailed":
    "These people weren't added: {names}. They stay in the picker, so you can select Add to retry.",
  "members.waiting": "Many changes in a short time. Adding the rest at {time}…",
  "members.col.name": "Name",
  "members.col.email": "Email",
  "members.col.addedBy": "Added by",
  "members.col.added": "Added",
  "members.addedByDefault": "Joined automatically at first sign-in",
  "members.addedByAdmin": "An administrator",
  "members.remove": "Remove",
  "members.removeName": "Remove {name}",
  "members.removeConfirm.title": "Remove {name} from {project}?",
  "members.removeConfirm.body":
    "They lose access to this project and its chats right away. Administrators keep access to every project.",
  "members.removeConfirm.action": "Remove",
  "members.removed": "{name} removed.",
  "members.empty": "No members yet. Add people so they can chat in this project.",
  "projectIndexes.map": "Map an index",
  "projectIndexes.picker": "Index",
  "projectIndexes.makeDefaultSwitch": "Make this the default",
  "projectIndexes.mapAction": "Map",
  "projectIndexes.col.index": "Index",
  "projectIndexes.col.default": "Default",
  "projectIndexes.col.status": "Status",
  "projectIndexes.defaultBadge": "Default",
  "projectIndexes.makeDefault": "Make default",
  "projectIndexes.unmap": "Unmap",
  "projectIndexes.unmapConfirm.title": "Unmap {index} from {project}?",
  "projectIndexes.unmapConfirm.body":
    "Chats in this project stop using this index. Its documents aren't deleted.",
  "projectIndexes.unmapConfirm.action": "Unmap",
  "projectIndexes.empty": "No indexes mapped. Map an index so people can chat with its documents.",
  "projectIndexes.noneAvailable":
    "No index can be mapped right now. Indexes already mapped to this project, and indexes that failed or are being deleted, aren't listed.",
  "projectIndexes.mapped": "Index mapped.",
  "projectIndexes.defaultSet": "Default index changed.",
  "projectIndexes.unmapped": "Index unmapped.",
  "admin.autoRefresh": "Auto-refresh",
  "admin.poll.stale": "Couldn't refresh. Showing results from {time}.",
  "admin.poll.stopped": "Automatic refresh stopped because DocChat couldn't be reached.",
  "admin.poll.refresh": "Refresh",
  "indexes.title": "Indexes",
  "indexes.new": "New index",
  "indexes.col.name": "Name",
  "indexes.col.method": "Method",
  "indexes.col.embeddingModel": "Embedding model",
  "indexes.col.documents": "Documents",
  "indexes.col.status": "Status",
  "indexes.embeddingValue": "{model}, {dimensions} dimensions",
  "indexes.empty": "No indexes yet. Create one, then upload documents to it.",
  "indexes.loadError": "Indexes didn't load.",
  "index.new.title": "New index",
  "index.form.name": "Name",
  "index.form.description": "Description",
  "index.form.method": "Ingestion method",
  "index.form.method.hint":
    "How documents are split into searchable passages. Changing it later means re-ingesting every document.",
  "index.form.params": "Method settings",
  "index.form.paramsUnsupported":
    "This ingestion method has settings this page can't show. Choose another method, or ask a developer to update the method's settings.",
  "index.form.embedding": "Embedding model",
  "index.form.dimensions": "Vector dimensions",
  "index.form.dimensions.hint": "Fewer dimensions use less space; more can find closer matches.",
  "index.form.language": "Language",
  "index.form.language.hint": "The language most of the documents are written in.",
  "index.create": "Create index",
  "index.created": "Index created. Upload documents to start ingestion.",
  "index.delete": "Delete index",
  "index.deleteConfirm.title": "Delete {name}?",
  "index.deleteConfirm.body":
    "Its documents and search data are removed for good. No project uses this index. You can't undo this.",
  "index.deleteConfirm.action": "Delete",
  "index.deleted": "{name} is being deleted.",
  "index.optionsError": "The index options didn't load.",
  "index.edit": "Edit settings",
  "index.save": "Save changes",
  "index.saved": "Settings saved.",
  "index.changeWarning":
    "Saving this change means every document must be re-ingested before it applies. Answers keep using the current version until then.",
  "index.reingestBanner.title": "Re-ingest needed",
  "index.reingestBanner.body":
    "This index's settings changed. Re-ingest its documents to apply them. Answers keep using the current version until then.",
  "index.reingest": "Start re-ingest",
  "index.reingestConfirm.title": "Re-ingest every document in {name}?",
  "index.reingestConfirm.body":
    "Each document is processed with the current settings. Answers keep using the current version of each document until its new version is ready.",
  "index.reingestConfirm.action": "Start re-ingest",
  "index.reingestStarted": "Re-ingest started.",
  "index.viewProgress": "View progress",
  "index.documents": "Documents",
  "index.runs": "Ingestion runs",
  "index.rebuildProgress": "Rebuilding: {ready} of {total} documents ready",
  "index.summary.method": "Method",
  "index.summary.settings": "Settings",
  "index.summary.embeddingModel": "Embedding model",
  "index.summary.language": "Language",
  "index.summary.status": "Status",
  "index.summary.documents": "Documents",
  "documents.title": "Documents in {index}",
  "documents.project": "Upload through project",
  "documents.project.hint":
    "Uploads are recorded against this project. Only projects that use this index are listed.",
  "documents.project.none":
    "No project uses this index yet. Map it to a project before uploading documents.",
  "documents.project.noneLink": "Go to Projects",
  "documents.status": "Ingestion status",
  "documents.upload": "Upload documents",
  "documents.filter": "Filter by name",
  "documents.stateFilter": {
    label: "State",
    options: {
      all: "All",
      ingested: "Ingested",
      in_progress: "In progress",
      failed: "Failed",
    },
  },
  "documents.col.name": "Name",
  "documents.col.version": "Version",
  "documents.col.state": "State",
  "documents.col.size": "Size",
  "documents.col.updated": "Updated",
  "documents.reingest": "Re-ingest",
  "documents.reingestStarted": "Re-ingest started for {name}.",
  "documents.delete": "Delete",
  "documents.deleteConfirm.title": "Delete {name}?",
  "documents.deleteConfirm.body":
    "It's removed from this index and from answers in every project that uses the index. You can't undo this.",
  "documents.deleteConfirm.action": "Delete",
  "documents.deleted": "{name} deleted.",
  "documents.empty": "No documents yet. Upload PDF, Word or PowerPoint files to add them.",
  "documents.noMatches": "No documents match these filters.",
  "documentPanel.versions": "Versions",
  "documentPanel.col.version": "Version",
  "documentPanel.col.state": "State",
  "documentPanel.col.uploaded": "Uploaded",
  "documentPanel.col.uploadedBy": "Uploaded by",
  "documentPanel.col.throughProject": "Through project",
  "documentPanel.col.size": "Size",
  "documentPanel.col.scanResult": "Scan result",
  "documentPanel.col.problem": "Problem",
  "documentPanel.close": "Close",
  "documentPanel.downloadPdf": "Download PDF",
  "documentPanel.pdfTooLarge":
    "This document is too large to download as one PDF. Its pages still open from chat citations.",
  "runs.title": "Ingestion runs",
  "runs.filter.index": "Index",
  "runs.filter.indexAll": "All indexes",
  "runs.filter.state": "State",
  "runs.filter.stateAll": "All states",
  "runs.col.started": "Started",
  "runs.col.type": "Type",
  "runs.col.index": "Index",
  "runs.col.startedBy": "Started by",
  "runs.col.progress": "Progress",
  "runs.col.state": "State",
  "runs.progress": "{done} of {total} documents",
  "runs.progressFailed": "{failed} failed",
  "runs.empty":
    "No ingestion runs yet. Runs appear here when you upload, re-ingest or delete documents.",
  "runs.noMatches": "No runs match these filters.",
  "runs.announce.finished": "Run finished: {state}.",
  "run.title": "Ingestion run",
  "run.field.state": "State",
  "run.field.type": "Type",
  "run.field.index": "Index",
  "run.field.startedBy": "Started by",
  "run.field.started": "Started",
  "run.field.finished": "Finished",
  "run.notFinished": "Not finished",
  "run.system": "System",
  "run.items": "Documents in this run",
  "run.col.document": "Document",
  "run.col.version": "Version",
  "run.col.stage": "Stage",
  "run.col.state": "State",
  "run.col.attempts": "Attempts",
  "run.col.problem": "Problem",
  "run.problem": "{message} ({code})",
  "chat.heading": {
    new: "Chat",
    session: "Chat: {title}",
  },
  "chat.sessions.label": "Chats",
  "chat.sessions.open": "Chats",
  "chat.sessions.new": "New chat",
  "chat.sessions.empty": "No chats yet. Your conversations will appear here.",
  "chat.sessions.more": "Show more chats",
  "chat.sessions.error": "Your chats didn't load.",
  "chat.sessions.answering": "Answering…",
  "chat.project.label": "Project",
  "chat.project.placeholder": "Choose a project",
  "chat.project.chooseFirst": "Choose a project to start asking questions.",
  "chat.project.changeHint": "Choosing a different project starts a new chat.",
  "chat.project.notReadySuffix": "(no documents yet)",
  "chat.project.notReady":
    "This project has no documents yet, so it can't answer questions. Ask your DocChat administrator to add documents to it.",
  "chat.projects.error": "Your projects didn't load.",
  "chat.empty.title": "What would you like to know?",
  "chat.empty.body":
    "Ask a question and DocChat answers from this project's documents, with numbered sources you can open.",
  "chat.empty.examples": "Try one of these",
  "chat.composer.label": "Your question",
  "chat.composer.placeholder": "Ask a question about this project's documents",
  "chat.composer.hint": "Press Enter to send. Press Shift+Enter for a new line.",
  "chat.composer.counter": "{count} of 4,000 characters",
  "chat.composer.tooLong": "Questions can be up to 4,000 characters. Shorten yours, then send it.",
  "chat.composer.reason.empty": "Type a question with at least one letter, number or symbol.",
  "chat.composer.reason.invalid_characters":
    "Your question contains characters DocChat can't accept. Remove them, then send it.",
  "chat.composer.send": "Send question",
  "chat.composer.stop": "Stop answer",
  "chat.composer.blockedUntil": "You can send your next question at {time}.",
  "chat.conversation.label": "Conversation",
  "chat.turn.youAsked": "You asked",
  "chat.turn.answerLabel": "DocChat",
  "chat.turn.searching": "Searching your documents…",
  "chat.turn.broadened": "Searching more broadly for better matches",
  "chat.turn.sources": "Sources",
  "chat.turn.citation": "Source {n}: {title}",
  "chat.locator.page": "page {page}",
  "chat.turn.linkSuffix": "(opens in a new tab)",
  "chat.turn.linkRemoved": "[link removed]",
  "chat.turn.table": "Table {k}",
  "chat.turn.helpful": "Helpful",
  "chat.turn.notHelpful": "Not helpful",
  "chat.turn.copy": "Copy answer",
  "chat.turn.copied": "Answer copied",
  "chat.turn.copyFailed": "Couldn't copy the answer. Select the text and copy it instead.",
  "chat.turn.stopped": "You stopped this answer.",
  "chat.turn.stoppedStored": "This answer was stopped before it finished.",
  "chat.turn.truncated":
    "This answer reached its length limit, so it may be incomplete. Ask a narrower question to get the rest.",
  "chat.turn.failedStored": "This answer couldn't be completed.",
  "chat.turn.inProgress": "This answer is still being written.",
  "chat.turn.inProgressLong": "This answer is taking longer than expected. Refresh to check on it.",
  "chat.turn.refresh": "Refresh",
  "chat.turn.autoRefresh": "Auto-refresh",
  "chat.turn.retry": "Retry",
  "chat.followups.label": "Suggested questions",
  "chat.earlier.load": "Show earlier messages",
  "chat.earlier.error": "Earlier messages didn't load.",
  "chat.jumpToLatest": "Jump to latest",
  "chat.unavailable.action": "New chat",
  "chat.error.title": "The answer couldn't be completed",
  "chat.error.cutOff":
    "The connection closed before the answer finished. Select Retry to send the question.",
  "chat.error.stalled": "The answer stopped arriving. Select Retry to send the question.",
  "chat.error.unreadable":
    "DocChat sent a reply this page couldn't read. Reload the page, then send the question.",
  "chat.error.network": "The connection was interrupted. Check your connection, then select Retry.",
  "chat.announce.done": {
    zero: "Answer complete. No sources were cited.",
    one: "Answer complete. 1 source.",
    other: "Answer complete. {count} sources.",
  },
  "chat.announce.stopped": "Answer stopped.",
  "chat.announce.failed": "The answer couldn't be completed.",
  "citation.title": "Source {n}",
  "citation.close": "Close source",
  "citation.loading": "Loading source…",
  "citation.frame": "Source {n}: {title}, {locator}",
  "citation.type.pdf": "PDF document",
  "citation.type.docx": "Word document",
  "citation.type.pptx": "PowerPoint presentation",
  "citation.field.type": "Type",
  "citation.field.location": "Location",
  "citation.field.file": "File",
  "citation.noPdfViewer": "Your browser can't show PDFs here. Download the page to open it.",
  "citation.downloadPage": "Download page",
  "citation.downloadDocument": "Download full document",
  "citation.documentTooLarge":
    "This document is too large to download as one file. Its cited pages still open here.",
  "citation.downloading": "Preparing download…",
  "citation.downloadFailed": "The download didn't finish. Select Retry.",
  "citation.error": "The source didn't load. Select Retry.",
  "feedback.title": "What went wrong?",
  "feedback.titleUp": "What made this answer helpful?",
  "feedback.addComment": "Add a comment",
  "feedback.editComment": "Edit your comment",
  "feedback.category.label": "Problem (optional)",
  "feedback.category.incorrect": "Incorrect",
  "feedback.category.incomplete": "Incomplete",
  "feedback.category.notRelevant": "Not relevant",
  "feedback.category.other": "Other",
  "feedback.comment.label": "Comment (optional)",
  "feedback.comment.hint": "Don't include passwords or other private details.",
  "feedback.comment.counter": "{count} of {max} characters",
  "feedback.submit": "Send feedback",
  "feedback.sending": "Sending…",
  "feedback.cancel": "Cancel",
  "feedback.saved": "Thanks. Your feedback was saved.",
  "feedback.reason.category_requires_down":
    "A problem type can only be chosen for an answer that wasn't helpful.",
  "feedback.reason.too_long":
    "Comments can be up to {max} characters. Shorten yours, then send it.",
  "feedback.reason.comments_disabled":
    "Comments are turned off by your DocChat administrator, so this comment can't be saved. Remove it to send your rating.",
  "upload.title": "Upload documents",
  "upload.choose": "Choose files",
  "upload.drop":
    "Or drop files here. PDF, Word (.docx) and PowerPoint (.pptx) files, up to {maxSize} each.",
  "upload.checking": "Checking {done} of {total} files…",
  "upload.tooMany": "Choose up to {max} files at a time.",
  "upload.col.file": "File",
  "upload.col.result": "Result",
  "upload.result.new": "New",
  "upload.result.unchanged": "Already uploaded. Skipped.",
  "upload.result.duplicate": "Same file as {existingName}. Skipped.",
  "upload.result.update": "This document is already ingested. Update it?",
  "upload.result.updateLabel": "Update {name}",
  "upload.result.rejectedType": "Not a PDF, Word or PowerPoint file. Skipped.",
  "upload.result.rejectedSize": "Larger than {maxSize}. Skipped.",
  "upload.result.rejected": "Can't be uploaded. Skipped.",
  "upload.result.unreadable":
    "This file couldn't be read. Check it, then choose it in a new selection.",
  "upload.result.empty": "This file is empty. Skipped.",
  "upload.result.invalidName":
    "This file name can't be used. Rename the file, then choose it in a new selection.",
  "upload.result.duplicateInBatch": "This file is already in this batch. Skipped.",
  "upload.nothingToUpload": "Nothing to upload. Every file was skipped.",
  "upload.sharedIndex": {
    one: "Everyone in the project that uses this index can find these documents in chat: {projects}.",
    other:
      "Everyone in the {count} projects that use this index can find these documents in chat: {projects}.",
  },
  "upload.sharedIndex.more": "and {count} more",
  "upload.sharedIndex.unknown":
    "Everyone in every project that uses this index can find these documents in chat.",
  "upload.start": {
    one: "Upload 1 file",
    other: "Upload {count} files",
  },
  "upload.progress": "Uploading {name}: {percent}%",
  "upload.fileDone": "Uploaded",
  "upload.fileFailed": "Not uploaded. {reason}",
  "upload.fileStopped": "Not uploaded. You stopped the upload.",
  "upload.stalled": "The upload stopped making progress.",
  "upload.stop": "Stop uploading",
  "upload.filePaused": "Not uploaded. Uploads are paused.",
  "upload.complete.title": "Upload complete",
  "upload.complete.body": {
    one: "Ingestion has started for 1 file.",
    other: "Ingestion has started for {count} files.",
  },
  "upload.partial":
    "{uploaded} of {total} files uploaded. Ingestion has started for those. Choose the others in a new batch to upload them.",
  "upload.viewProgress": "View progress",
  "upload.close": "Close",
  "error.reference": "Reference ID: {id}",
  "error.copyReference": "Copy reference ID",
  "error.referenceCopied": "Reference ID copied",
  "error.retry": "Retry",
  "error.retryAt": "You can retry at {time}.",
  "error.network.title": "Can't reach DocChat",
  "error.network.body": "Check your connection, then select Retry.",
  "error.server.title": "Something went wrong",
  "error.server.body":
    "DocChat had a problem on its side. Retry in a few minutes. If it keeps happening, send the reference ID to your DocChat administrator.",
  "error.unexpected.title": "Unexpected reply",
  "error.unexpected.body":
    "DocChat sent a reply this page couldn't read. Reload the page, then retry.",
  "error.validation.field": "This value isn't valid. Check it and correct it.",
  "error.sessionExpired": "You need to sign in before this can finish.",
  "error.defect.body":
    "Reload the page. If this keeps happening, send the reference ID to your DocChat administrator.",
  "app.reload": "Reload",
  "access.retry": "Retry",
  "access.signOut": "Sign out",
};

/** Display names for API values (10-web-client.md section 7.12). Each record is keyed by the
 * generated enumeration, so a value the API adds fails `tsc` until it has its name here. */
export const displayNames: {
  readonly runState: Readonly<Record<RunSummary["state"], string>>;
  readonly runKind: Readonly<Record<RunSummary["kind"], string>>;
  readonly itemState: Readonly<Record<RunItem["state"], string>>;
  readonly itemOperation: Readonly<Record<RunItem["operation"], string>>;
  readonly itemStage: Readonly<Record<NonNullable<RunItem["stage"]>, string>>;
  readonly versionState: Readonly<Record<VersionSummary["state"], string>>;
  readonly documentDisplayState: Readonly<Record<DocumentSummary["displayState"], string>>;
  readonly scanStatus: Readonly<Record<VersionSummary["scanStatus"], string>>;
  readonly indexStatus: Readonly<Record<IndexSummary["status"], string>>;
  readonly projectStatus: Readonly<Record<AdminProjectSummary["status"], string>>;
  readonly factCategory: Readonly<Record<FactCategory, string>>;
  readonly feedbackCategory: Readonly<Record<FeedbackCategory, string>>;
  readonly mediaType: Readonly<Record<ContentType, string>>;
} = {
  runState: {
    queued: "Queued",
    running: "Running",
    completed: "Completed",
    completed_with_errors: "Completed with errors",
    failed: "Failed",
  },
  runKind: {
    upload: "Upload",
    document_reingest: "Re-ingest document",
    document_delete: "Delete document",
    index_create: "Create index",
    index_reingest: "Re-ingest index",
    index_rebuild: "Rebuild index",
    index_delete: "Delete index",
    maintenance: "Maintenance",
  },
  itemState: {
    queued: "Queued",
    running: "Running",
    succeeded: "Done",
    skipped: "Skipped",
    failed: "Failed",
    cancelled: "Cancelled",
  },
  itemOperation: {
    ingest: "Ingest",
    cleanup_document: "Clean up document",
    rerender: "Refresh page previews",
    create_physical_index: "Create search index",
    delete_physical_index: "Delete search index",
    purge_index_blobs: "Remove index files",
  },
  itemStage: {
    validate: "Validate",
    extract: "Extract",
    enrich: "Enrich",
    chunk: "Chunk",
    embed: "Embed",
    index: "Index",
    render: "Render pages",
    flip: "Publish",
    clean_up: "Clean up",
    purge_document: "Remove document",
    create_index: "Create search index",
    delete_index: "Delete search index",
    purge_blobs: "Remove files",
  },
  versionState: {
    uploading: "Uploading",
    pending: "Waiting",
    superseded: "Replaced",
    ingesting: "Ingesting",
    ingested: "Ingested",
    failed: "Failed",
  },
  documentDisplayState: {
    ingested: "Ingested",
    in_progress: "In progress",
    failed: "Failed",
    no_content: "No content",
  },
  scanStatus: {
    not_required: "Not required",
    awaiting: "Waiting for scan",
    clean: "No threats found",
    malicious: "Malware found",
    failed: "Scan failed",
    unavailable: "Scan unavailable",
  },
  indexStatus: {
    provisioning: "Setting up",
    ready: "Ready",
    reingest_required: "Re-ingest needed",
    reingesting: "Re-ingesting",
    rebuilding: "Rebuilding",
    failed: "Failed",
    deleting: "Deleting",
    deleted: "Deleted",
  },
  projectStatus: { active: "Active", archived: "Archived" },
  factCategory: {
    role: "Role",
    team: "Team",
    expertise: "Expertise",
    topic: "Topic",
    answer_style: "Answer style",
  },
  feedbackCategory: {
    incorrect: "Incorrect",
    incomplete: "Incomplete",
    not_relevant: "Not relevant",
    other: "Other",
  },
  mediaType: {
    "application/pdf": "PDF document",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "Word document",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation":
      "PowerPoint presentation",
  },
};
