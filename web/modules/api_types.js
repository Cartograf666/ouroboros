/** Dependency-free JSDoc mirror of `ouroboros.gateway.contracts`. */

/**
 * @typedef {Object} StateResponse
 * @property {number} uptime
 * @property {number} workers_alive
 * @property {number} workers_total
 * @property {number} pending_count
 * @property {number} running_count
 * @property {?number} spent_usd
 * @property {number} budget_limit
 * @property {?number} budget_pct
 * @property {string} branch
 * @property {string} sha
 * @property {boolean} evolution_enabled
 * @property {boolean} bg_consciousness_enabled
 * @property {number} evolution_cycle
 * @property {Object} evolution_state
 * @property {Object} bg_consciousness_state
 * @property {?number} spent_calls
 * @property {boolean} supervisor_ready
 * @property {?string} supervisor_error
 * @property {string} runtime_mode
 * @property {string} context_mode
 * @property {boolean} context_mode_auto_low  // effective low is a system auto-downgrade, not an owner selection
 * @property {string} safety_mode
 * @property {boolean} skills_repo_configured
 * @property {boolean} github_token_configured
 * @property {Object} accounting  // physical-attempt ledger projection
 * @property {Array<Object>} projects  // active/deleting ProjectEntry sidebar projection
 * @property {Array<number>} project_chat_ids  // complete (uncapped) project chat_ids — WS fan-out isolation SSOT (v6.32.0)
 * @property {Object<string, {project_id: string, chat_id: number}>} task_bindings  // bound task -> its project: suppress the stray "turn into project" button (v6.33.0 P2) + render a pointer that opens the project panel (v6.33.0 F4)
 */

/**
 * @typedef {Object} EvolutionDataResponse
 * @property {Object[]} points
 * @property {Object[]=} checkpoints
 * @property {string} generated_at
 * @property {boolean} cached
 */

/**
 * @typedef {Object} HealthResponse
 * @property {"ok"} status
 * @property {string} version
 * @property {string} runtime_version
 * @property {string} app_version
 */

/**
 * @typedef {Object} OpenAICompatibleModelsResponse
 * @property {string[]} models
 * @property {string=} error
 */

/**
 * @typedef {Object} SettingsMeta
 * @property {string[]=} custom_secret_keys
 * @property {Object=} setup_contract
 */

/**
 * @typedef {Object} ChatInbound
 * @property {"chat"} type
 * @property {string} content
 * @property {string=} sender_session_id
 * @property {string=} client_message_id
 * @property {boolean=} force_plan
 * @property {Array<Object>=} attachments  // [{filename, display_name, mime}] — image uploads become native blocks (v6.26.0)
 * @property {number=} chat_id     // multi-project thread routing (v6.32.0); main chat = 1
 * @property {string=} project_id  // per-project memory scope (v6.32.0)
 */

/**
 * @typedef {Object} CommandInbound
 * @property {"command"} type
 * @property {string} cmd
 */

/**
 * @typedef {Object} ChatOutbound
 * @property {"chat"} type
 * @property {"user"|"assistant"|"system"} role
 * @property {string} content
 * @property {string} ts
 * @property {boolean=} markdown
 * @property {boolean=} is_progress
 * @property {string=} task_id
 * @property {boolean=} ephemeral_decision
 * @property {string=} task_incident
 * @property {string=} toast_once
 * @property {Object=} lifecycle
 * @property {string=} subagent_event
 * @property {string=} subagent_task_id
 * @property {string=} root_task_id
 * @property {string=} parent_task_id
 * @property {string=} delegation_role
 * @property {string=} subagent_role
 * @property {boolean=} accepted
 * @property {number=} active_subagent_count
 * @property {number=} max_active_subagents
 * @property {boolean=} queued_behind_active_cap
 * @property {string[]=} required_capabilities
 * @property {string=} write_surface
 * @property {string=} model_lane
 * @property {string=} requested_model_lane
 * @property {string=} effective_model_lane
 * @property {string=} model
 * @property {string=} task_group_id
 * @property {string=} task_event
 * @property {string=} status
 * @property {boolean=} cancelable
 *   v6.82 (P5): host-attested — this frame's task is a supervisor-queue task that
 *   POST /api/tasks/{id}/cancel can force-cancel (never set for direct-chat turns).
 * @property {?number=} cost_usd
 * @property {"available"|"unavailable"=} cost_accounting_status
 * @property {string=} cost_accounting_error
 * @property {boolean=} cost_final
 * @property {?number=} cost_usd_with_children
 * @property {boolean=} cost_with_children_partial
 * @property {?number=} reserved_usd
 * @property {?number=} unresolved_upper_bound_usd
 * @property {?number=} unknown_unmetered
 * @property {string=} result
 * @property {boolean=} result_truncated
 * @property {string=} trace_summary
 * @property {boolean=} trace_summary_truncated
 * @property {string=} error
 * @property {string=} artifact_status
 * @property {Object=} artifact_bundle
 * @property {Object=} outcome_axes
 * @property {Object=} task_contract
 * @property {string=} reason_code
 * @property {Object=} review_status
 * @property {Object=} review_projection
 *   v6.74.0 additive keys: panels[].dialogue ({status, votes} — the reviewer-authored
 *   dialogue-status reduction), panels[].single_reviewer_no_diversity (boolean label),
 *   and actors[].dialogue_status ("continue_actionable"|"unreachable_here"|"stable_disagreement"|"").
 * @property {boolean=} worker_saturation_warning
 * @property {string=} source
 * @property {string=} sender_label
 * @property {string=} sender_session_id
 * @property {string=} client_message_id
 * @property {Object=} transport
 * @property {number=} telegram_chat_id
 * @property {string=} system_type
 * @property {number=} chat_id
 */

/**
 * @typedef {Object} PhotoOutbound
 * @property {"photo"} type
 * @property {"user"|"assistant"} role
 * @property {string} image_base64
 * @property {string} mime
 * @property {string} ts
 * @property {string=} caption
 * @property {string=} content
 * @property {string=} source
 * @property {string=} sender_label
 * @property {string=} sender_session_id
 * @property {string=} client_message_id
 * @property {Object=} transport
 * @property {number=} chat_id
 * @property {number=} telegram_chat_id
 */

/**
 * @typedef {Object} VideoOutbound
 * @property {"video"} type
 * @property {"user"|"assistant"} role
 * @property {string} video_base64
 * @property {string} mime
 * @property {string} ts
 * @property {string=} caption
 * @property {string=} content
 * @property {string=} source
 * @property {string=} sender_label
 * @property {string=} sender_session_id
 * @property {string=} client_message_id
 * @property {Object=} transport
 * @property {number=} chat_id
 * @property {number=} telegram_chat_id
 */

/**
 * @typedef {Object} DocumentOutbound
 * @property {"document"} type
 * @property {"user"|"assistant"} role
 * @property {string} file_base64
 * @property {string} mime
 * @property {string} filename
 * @property {string} ts
 * @property {string=} caption
 * @property {string=} download_url
 * @property {string=} content
 * @property {string=} source
 * @property {string=} sender_label
 * @property {string=} sender_session_id
 * @property {string=} client_message_id
 * @property {Object=} transport
 * @property {number=} chat_id
 * @property {number=} telegram_chat_id
 */

/**
 * @typedef {Object} LogOutbound
 * @property {"log"} type
 * @property {Object} data
 * @property {number=} chat_id  // multi-project thread routing (v6.32.0); main chat = 1
 */

/**
 * @typedef {Object} ProjectsChangedOutbound
 * @property {"projects_changed"} type
 * @property {string=} project_id
 * @property {number=} chat_id  // new project thread; client learns it before /api/state (v6.32.0)
 */

/**
 * Bubble-free presentation update for an existing owner message.
 * @typedef {Object} MessageAnnotationOutbound
 * @property {"message_annotation"} type
 * @property {"routing_ack"} annotation_type
 * @property {number=} chat_id
 * @property {string} client_message_id
 * @property {string} action
 * @property {string=} target
 * @property {string} status
 * @property {Array<Object>=} options
 * @property {boolean} suppress_bubble
 * @property {string=} ts
 */

/**
 * Additive /api/chat/history row fields (v6.73.0 Project origin projection).
 * A Project thread may synthesize its start message from the binding's own
 * durable copy when the canonical row left the bounded read window:
 * `origin_projected: true` marks such a synthesized user row (normal history
 * shape otherwise), and a `system_type: "origin_omission"` system row discloses
 * origins omitted past the synthesis cap. Both fields are additive and safely
 * ignorable by renderers.
 * @typedef {Object} ProjectOriginHistoryFields
 * @property {boolean=} origin_projected
 * @property {"origin_omission"=} system_type
 */

/**
 * POST /api/projects body (v6.59.0). ONE source: path (attach; optional init_git
 * attach-snapshot commit — never auto-init), git_url (server-side clone; typed
 * auth_required), with_workspace (genesis), or none (file-less).
 * @typedef {Object} ProjectCreateRequest
 * @property {string=} id
 * @property {string=} name
 * @property {string=} path
 * @property {boolean=} init_git
 * @property {string=} git_url
 * @property {boolean=} with_workspace
 */

/**
 * @typedef {Object} ProjectEntry
 * @property {string} id
 * @property {string=} name
 * @property {number=} chat_id
 * @property {string=} working_dir
 * @property {string=} provenance   // attached | cloned | genesis | none (historical fact)
 * @property {string=} clone_url
 * @property {string=} trusted_at
 * @property {string=} last_active_at
 * @property {"active"|"deleting"|"tombstoned"=} lifecycle
 * @property {number=} routing_generation
 * @property {number=} visible_revision
 * @property {string=} delete_error
 */

/**
 * @typedef {Object} ProjectDeleteResponse
 * @property {boolean} ok
 * @property {string} project_id
 * @property {boolean} folder_untouched
 */

/**
 * GET /api/fs/dirs — server-side directory browser (New Project attach picker).
 * @typedef {Object} FsDirsEntry
 * @property {string} name
 * @property {string} path
 * @property {boolean} is_git
 */

/**
 * @typedef {Object} FsDirsResponse
 * @property {string} path
 * @property {string} parent
 * @property {string} home
 * @property {FsDirsEntry[]} dirs
 * @property {boolean} truncated  // true when the dir holds more children than the 500-entry cap
 */

/**
 * @typedef {Object} TaskNamedOutbound
 * @property {"task_named"} type
 * @property {string} task_id
 * @property {string} suggested_name  // proactively-coined project name; client sets the live card title (v6.40.0)
 */

/**
 * @typedef {Object} UploadResponse
 * @property {boolean} ok
 * @property {string} filename
 * @property {string} display_name
 * @property {string} path
 * @property {number} size
 * @property {string} mime
 */

/**
 * @typedef {Object} OwnerRuntimeModeResponse
 * @property {boolean} ok
 * @property {string} runtime_mode
 * @property {boolean} restart_required
 */

/**
 * @typedef {Object} OwnerAutoGrantResponse
 * @property {boolean} ok
 * @property {boolean} enabled
 */

/**
 * @typedef {Object} OwnerContextModeResponse
 * @property {boolean} ok
 * @property {string} context_mode
 */

/**
 * @typedef {Object} OwnerScopeReviewFloorResponse
 * @property {boolean} ok
 * @property {string} scope_review_floor  // blocking_1m | advisory (v6.34.0, CW1)
 * @property {string} deprecation_notice  // v6.80.0: stored, but enforcement-inert
 */

/**
 * @typedef {Object} OwnerSafetyModeResponse
 * @property {boolean} ok
 * @property {string} safety_mode  // full | light | off (v6.54.3)
 */

/**
 * @typedef {Object} InstalledSkill
 * @property {string} name
 * @property {string} type
 * @property {string=} version
 * @property {string=} description
 * @property {boolean=} enabled
 * @property {string=} source
 * @property {string=} payload_root
 * @property {string=} review_status
 * @property {boolean=} review_stale
 * @property {Object=} review_gate
 * @property {boolean=} executable_review
 * @property {string=} review_profile
 * @property {boolean=} official_hub_verified
 * @property {boolean=} owner_attestable
 * @property {{visible: boolean, disabled: boolean, reason: string}=} submit_hub
 * @property {{current: Object, history: Object[]}=} skill_review
 * @property {boolean=} is_self_authored
 * @property {Object=} grants
 * @property {string[]=} permissions
 * @property {string[]=} conflicts
 * @property {{code: "skill_conflict", skills: string[], omitted: number}=} conflict
 */

/**
 * @typedef {Object} SkillGrantResponse
 * @property {boolean} ok
 * @property {string} skill
 * @property {string[]=} granted_keys
 * @property {string[]=} granted_permissions
 * @property {string=} extension_action
 * @property {string=} extension_reason
 * @property {string=} load_error
 * @property {Object=} grants
 */

/**
 * @typedef {Object} ExecutorRef
 * @property {"local"|"docker_exec"} type
 * @property {string=} id
 * @property {"host"|"none"=} network
 * @property {string=} workspace_host_path
 * @property {string=} workspace_backend_path
 * @property {string=} container_name Required when type is "docker_exec".
 * @property {Object[]=} path_mappings
 */

/**
 * @typedef {Object} TaskCreateRequest
 * @property {string} description
 * @property {string=} task_id
 * @property {string=} type
 * @property {number=} chat_id
 * @property {number=} depth
 * @property {string=} session_id
 * @property {string=} workspace_root
 * @property {"external"=} workspace_mode
 * @property {"forked"|"empty"|"shared"=} memory_mode
 * @property {string=} project_id Per-project facts scope id (else derived from the workspace path).
 * @property {Object[]=} attachments
 * @property {Object[]=} acceptance_claims Advisory Observable Acceptance Claims (`claim`/`surface`/`support`/`priority`).
 * @property {string=} answer_protocol  // "" | "final_answer_line" — machine-extractable answer line (v6.60.0)
 * @property {Object=} allowed_resources
 * @property {Object=} resource_policy
 * @property {string[]=} disabled_tools Declarative tool-policy denylist: tool names withheld from the agent (independent of allowed_resources).
 * @property {ExecutorRef=} executor_ref
 * @property {"stop"|"keep"=} service_teardown Task service finalization policy; `keep` is for external verifiers/owners that need live services after task completion. POSIX-only: on Windows a cancel/hard-timeout tree-kills all task processes, so `keep` is not preserved there.
 * @property {string=} deadline_at
 * @property {number=} timeout_sec
 * @property {number=} timeout
 * @property {string=} context
 * @property {string=} expected_output
 * @property {string=} constraints
 * @property {boolean=} context_requires_self_body_docs
 * @property {string=} actor_id Top-level task actor/provenance id; metadata.actor_id is reserved.
 * @property {string=} source Top-level task source/provenance label.
 * @property {Object=} metadata Arbitrary task metadata; executor_ref/workspace_executor keys are reserved.
 */

/**
 * @typedef {Object} TaskCreateResponse
 * @property {boolean} ok
 * @property {string} task_id
 * @property {string} status
 */

/**
 * @typedef {Object} TaskEvent
 * @property {number} seq
 * @property {string=} source
 * @property {number=} line
 * @property {string} type
 * @property {string} task_id
 * @property {string=} ts
 * @property {string=} root
 * @property {Object=} data
 */

/**
 * @typedef {Object} TaskListResponse
 * @property {Object[]} tasks
 * @property {Object=} queue
 */

/**
 * @typedef {Object} ScheduledTasksResponse
 * @property {number} schema_version
 * @property {Object[]} tasks
 */

/**
 * @typedef {Object} ScheduleUpsertResponse
 * @property {boolean} ok
 * @property {Object} schedule
 */

/**
 * @typedef {Object} ScheduleDeleteResponse
 * @property {boolean} ok
 */

/**
 * @typedef {Object} TaskCancelResponse
 * @property {boolean} ok
 * @property {string} task_id
 * @property {boolean=} cascade
 *   v6.82 (P5): echoed only when the request body {"cascade": true} asked for the
 *   subtree cancel, which is complete by the time this answer is sent; the plain
 *   single-task envelope is unchanged.
 */

/**
 * @typedef {Object} LogTailResponse
 * @property {string} name
 * @property {Object[]} entries
 */

/**
 * @typedef {Object} SkillDeleteResponse
 * @property {boolean} ok
 * @property {string} skill
 * @property {string} source
 * @property {string} deleted_payload_root
 * @property {boolean} deleted_state
 * @property {string} extension_action
 * @property {string} extension_reason
 * @property {string=} error
 */

/**
 * @typedef {Object} UiPreferencesResponse
 * @property {string[]} widget_order
 * @property {boolean} nested_subagents_expanded
 * @property {number} sidebar_width  // px; 0 = CSS default (v6.33.0)
 * @property {number} project_panel_width  // px; 0 = CSS default
 * @property {Object.<string,number>} project_seen_revision  // monotonic paint ACK
 * @property {Object.<string,string>} project_last_viewed  // deprecated accepted no-op
 * @property {Object.<string,boolean>} project_hidden  // deprecated accepted no-op
 * @property {boolean=} ok
 */

/**
 * @typedef {Object} UpdatePreflightProtectedRoute
 * @property {string} offered_strategy  // auto_merge | assisted
 * @property {boolean} will_route_manual
 * @property {string} reason
 *   v6.88.1: '' | 'protected_paths' | 'protected_delta_unverifiable'. An unverifiable
 *   official delta routes to manual with an EMPTY protected_paths list, so branch on the
 *   reason, never on the list being non-empty.
 * @property {string[]} protected_paths
 */

/**
 * @typedef {Object} UpdatePreflightSuccessResponse
 * @property {Object} merge_plan
 * @property {UpdatePreflightProtectedRoute} protected_route
 */

/**
 * @typedef {Object} UpdatePreflightErrorResponse
 * @property {string} error
 */

/**
 * The preflight endpoint's response. `error` REPLACES the success keys rather than accompanying
 * them — an unhandled exception answers the shared error envelope — so this is a union, not one
 * object with an optional field. Declared the old way, the success keys were required on a frame
 * that never carries them.
 * @typedef {UpdatePreflightSuccessResponse|UpdatePreflightErrorResponse} UpdatePreflightResponse
 */

/**
 * @typedef {Object} UpdateApplyRequest
 * @property {string=} strategy
 *   auto_merge | assisted | doc_reconcile | manual | replace | stash | force
 * @property {boolean=} acknowledge_protected
 *   v6.88.1 BOUND acknowledgement of an official change to a protected tier other than
 *   frozen-contract / release-invariant (safety-critical or unrecognized) on the replace
 *   family: the three echoed fields below must match the disclosure exactly, or the
 *   backend keeps routing the apply to manual.
 * @property {string=} acknowledged_base_sha
 * @property {string=} acknowledged_target_sha
 * @property {string[]=} acknowledged_protected_paths
 */

/**
 * @typedef {Object} UpdateApplyOkResponse
 * @property {'ok'} status
 *   A LITERAL type, not `string`: this field discriminates the union below, and an unconstrained
 *   `string` made every variant structurally interchangeable so nothing could narrow on it.
 * @property {boolean} restarting
 *   v6.88.1: FALSE means the update landed and only the RESTART request failed — the checkout has
 *   already moved, so this is not a failure; `warning` says what is left to do by hand.
 * @property {string=} warning
 * @property {string=} strategy  // staged auto_merge only
 * @property {Object=} merge_plan  // staged auto_merge only
 * @property {Object=} rescue  // replace family only
 * @property {string=} keep_branch  // replace family only; '' when nothing was preserved
 */

/**
 * @typedef {Object} UpdateApplyAssistedStartedResponse
 * @property {'assisted_started'} status  // literal, see UpdateApplyOkResponse
 * @property {string} task_id
 * @property {Object} merge_plan
 */

/**
 * @typedef {Object} UpdateApplyManualResponse
 * @property {'manual'} status  // literal, see UpdateApplyOkResponse
 * @property {string=} reason
 *   '' (the owner asked for manual) | 'protected_paths' | 'protected_delta_unverifiable' |
 *   'release_moved'. Branch on the reason: an unverifiable delta and a fresh disclosure can both
 *   arrive with an empty protected_paths list and they mean opposite things.
 * @property {boolean=} requires_acknowledgement
 *   The protected DISCLOSURE. Re-post with the UpdateApplyRequest acknowledgement fields echoing
 *   base_sha / target_sha / protected_paths exactly, or the apply stays routed to manual.
 * @property {string[]=} protected_paths
 * @property {string=} base_sha
 * @property {string=} target_sha
 * @property {Object=} merge_plan
 */

/**
 * @typedef {Object} UpdateApplyErrorResponse
 * @property {string} error
 * @property {string=} reason
 *   'update_lock_held' | 'release_moved' | 'assisted_resolver_unavailable' | ...
 * @property {string=} plan_error
 *   The planner's own low-level message, kept separate from the endpoint's `error`. A
 *   plan-carrying error frame also splats the merge plan's own keys alongside these.
 * @property {string=} rollback
 *   What the undo of an already-landed mutation reported. A RESULT, not a status: a frame whose
 *   reason is 'update_recovery_failed' is one whose rollback could NOT be proven, so the presence
 *   of this key must never be read as "recovered".
 * @property {Object=} status
 *   THE reason `error` is checked first: a preparation refusal carries the managed-update status
 *   OBJECT under this key, so `status` on an error frame is a payload, not the discriminator. A
 *   consumer that switched on `status` first would read a truthy object here.
 */

/**
 * The apply endpoint's discriminated response. Check `error` FIRST — it replaces `status` rather
 * than accompanying it — then switch on `status`.
 * @typedef {UpdateApplyOkResponse|UpdateApplyAssistedStartedResponse|UpdateApplyManualResponse|UpdateApplyErrorResponse} UpdateApplyResponse
 */

export const GATEWAY_CONTRACT_VERSION = '6.88.1';
