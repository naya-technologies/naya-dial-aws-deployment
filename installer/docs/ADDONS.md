# Add-ons

An add-on is an optional product that runs next to the base DIAL platform: AI Brain today, others later. Add-ons come from
Naya's own registry, not from the AWS Marketplace. The base platform (Core, Chat, Admin, QuickApps, Flows, ...) is always installed; an
add-on is installed, upgraded and removed on its own and never changes the platform's version.

```
./dial-install add-ons list                 # what exists, what is installed
./dial-install add-ons install ai-brain     # copy the images, install, register in DIAL Core
./dial-install add-ons upgrade ai-brain     # move to the version in this package
./dial-install add-ons remove ai-brain      # remove everything the add-on created
```

All of them accept the common options (`-c`, `--profile`); `install`, `upgrade` and `remove` also accept `-y` and `--dry-run`,
`install` and `upgrade` accept `--model <id>` for add-ons that use a model, and `remove` accepts `--keep-images`. `addons` is an alias of `add-ons`.

## For customers

Run these from where you ran `apply` (CloudShell), in the folder with `dial.yaml`. DIAL must be installed, at the release `dial.yaml`
selects (`./dial-install status` shows it). If it is not, the command says so and does nothing.

**install** does, in this order, and stops at the first problem:

1. copies the add-on's images from Naya's registry into your private ECR (same step as `apply` uses for the platform images;
   what is already there is skipped). Naya's registry is public (Amazon ECR Public, no account or login needed); if a registry is private, your AWS
   account must be allowed to pull from it, and the message names it. With `registry.provider: public` nothing is copied and the cluster pulls from Naya's registry directly;
2. installs the add-on as its own Helm release `dial-addon-<id>` in the DIAL namespace, and waits until it is ready;
3. registers its application type(s) in DIAL Core: it upgrades the platform release with Core's configuration extended, and restarts
   Core once so that it reads it (about a minute; users may see a short interruption). Nothing else in the platform changes.

When it finishes, the new application type is offered wherever people create applications in DIAL.

**upgrade** installs the version in this package. Nothing happens when it is already at that version. Core is touched only if the
add-on's registration changed.

**remove** takes the add-on out of DIAL Core first (Core restarts, only if it registered something), then removes its Helm release and its node group, then deletes its image
repositories in your ECR (not with `--keep-images`). It asks you to type the add-on id. At the end it checks that nothing is left.
Applications that people created from the add-on's application type are **not deleted**: they stay in DIAL and stop working. Delete
them in DIAL first if you do not want them.

`uninstall` of the whole installation removes the add-ons too (including their node groups); `apply` and `upgrade` of the platform keep them registered.

### Which version is installed

Every time you run `install`, `upgrade` or `list`, the installer asks Naya's registry which is the newest version **at that moment**
(the highest `YYYY.M.N` tag; nothing is remembered between runs, and an old version is never used when the registry cannot be read).
To install a specific version instead: `--version <tag>`. `upgrade` moves to the newest one, or to the `--version` you name.

### Add-ons that use a model

`ai-rag` answers with a DIAL model. By default it uses the system default model, `default.model`. To use another model that DIAL Core
offers:

```
./dial-install add-ons install ai-rag --model <model id>
./dial-install add-ons upgrade ai-rag --model <other model id>     # change it later
```

The choice is remembered: a later `upgrade` without `--model` keeps it. The model must exist in DIAL (the id as DIAL Core knows it).

### Add-ons with their own nodes (AI RAG)

`ai-rag` needs 4 GiB of memory, so it gets a **node group of its own**, created by `install`: a separate CloudFormation stack
`<name>-addon-ai-rag`, nodes labelled and tainted so that nothing else is scheduled on them, and the add-on's pod tolerates exactly that. It cannot
take capacity from the platform and the platform cannot take it from RAG. Defaults: one `c6i.xlarge` (4 vCPU, 8 GiB). Change them with flags:

| Flag | Meaning |
|---|---|
| `--instance-type <type>` | EC2 instance type, for example `c6i.2xlarge` |
| `--nodes <n>` | Number of nodes, 1-10 (fixed: the add-on is not autoscaled) |
| `--no-dedicated-nodes` | Do not create a node group; run on the shared platform nodes (they are small: not recommended) |

The settings are remembered and an `upgrade` with a flag changes them. `remove` deletes the node group; so does `uninstall` of the whole installation.
Creating the node group takes a few minutes.

### What ai-rag registers in DIAL

An application, **DIAL X RAG** (`dial-x-rag`), in DIAL Core's configuration: endpoint `http://<release>-rag.<namespace>.svc.cluster.local/openai/deployments/dial-rag/chat/completions`,
icon `RAG_files_search.svg` from your themes host, with the features DIAL needs for files and tools. **Nobody can use it yet**: like a new model, it has no access roles (`userRoles: []`, exactly what DIAL Admin writes for an entity without roles). After installing, an administrator opens DIAL Admin, Applications, DIAL X RAG, and adds the roles that may use it (for example `admin`, or the groups of your users).
Core restarts once so that it reads it. If Core does not come back, the message prints the exact `helm rollback` command.

### Bedrock Guardrail interceptor (`bedrock-guardrail`)

Runs every chat message, and the model's answer, through an Amazon Bedrock guardrail (ApplyGuardrail) before it goes on. Create and
publish the guardrail in the Amazon Bedrock console first (a numbered version), then:

```
./dial-install add-ons install bedrock-guardrail --guardrail-id <id> --guardrail-version <number>
```

| Flag | Meaning |
|---|---|
| `--guardrail-id`, `--guardrail-version` | Required. The guardrail and its numbered version. `DRAFT` is refused unless `--allow-draft` (testing only). |
| `--bedrock-region` | Region of the guardrail. Default: the region of the installation. |
| `--guardrail-deployments a,b` | The DIAL deployments it checks. Default: `default.model` only. Any model the installer registers or any add-on application (`dial-x-rag`). |
| `--guardrail-global` | Check every deployment instead (DIAL Core `globalInterceptors`). Not the default: applications that call a model through Core would be checked twice (cost, latency, and possible blocks of internal calls). |

What `install` does, and stops at the first problem: checks that the guardrail exists (`bedrock get-guardrail`), copies the image
(version `1.0.1`, refused if the tag no longer points at the tested digest), creates the CloudFormation stack `<name>-addon-bedrock-guardrail`
with the interceptor's **own IAM role** (only its own service account may assume it; it may only call `bedrock:ApplyGuardrail` on this guardrail),
installs it with its own service account annotated with that role, registers it in DIAL Core (`interceptors`, endpoint with the full path
`/openai/deployments/bedrock-guardrail/chat/completions`, `forwardAuthToken: false`) and attaches it, then checks that it works: inside the pod
`sts get-caller-identity` must be that role, and a real chat request through Core to an attached deployment must return the model's answer.
`upgrade` with the same settings runs the checks again; with other settings (for example `--guardrail-global`) it changes them.

Security: the interceptor's AWS access comes only from its own role. No other pod gets it, the `default` service account is not touched, and
the container never gets `AWS_*` credentials (temporary SSO credentials included).

Answers arrive all at once instead of word by word while it is attached: the answer is checked before it is sent. That is expected.

## For the vendor: adding an add-on

Each add-on is a folder `addons/<id>/` with two files (`addon.yaml`, `values.yaml`), and code only when a step cannot be expressed in the catalog:
`hooks.py` with any of `validate`, `preflight`, `stack_parameters`, `verify` (see `addons/bedrock-guardrail/hooks.py`). The generic `dial-extension` chart (the same one the platform uses
for QuickApps and Flows) is shipped once in `addons/charts/` and is the default chart.

`addon.yaml` (format `dial-addon/v1`):

| Key | Meaning |
|---|---|
| `id` | Lower-case letters, digits, dashes; equal to the folder name. The Helm release is `<platform release>-addon-<id>`. |
| `display_name`, `description` | Shown by `list`. |
| `version` | `latest` (the default for every add-on: the newest `YYYY.M.N` tag in the registry when the command runs), or a tag to pin. |
| `source.registry` | Registry the images are copied from: `public.ecr.aws/<alias>` (Amazon ECR Public: anonymous pull, no login) or a private ECR host such as `<account>.dkr.ecr.<region>.amazonaws.com` (the customer's AWS account must be allowed to pull). |
| `stack` | Optional. `template:` (relative to `addons/`) of the add-on's own CloudFormation stack `<name>-addon-<id>`, for example an IAM role. Its parameters come from `stack_parameters` in `hooks.py`, its outputs fill `PLACEHOLDER_STACK_<OUTPUT>` (`RoleArn` -> `STACK_ROLE_ARN`). An add-on has a `stack` or a `node_group`, not both. |
| `core.interceptors`, `core.attach` | Optional. Interceptors registered in DIAL Core, and where they are attached: `deployments_option` (a setting with a comma-separated list) and `global_option` (`globalInterceptors`). |
| `options.<name>` | `default` (`{region}` = the installation's region) or `required: true`, plus `pattern`, `flag` (the command-line flag, added to `install` and `upgrade` automatically) and `description`. |
| `images[].digest` | Optional. The copy is refused when the tag points at another image. |
| `node_group` | Optional. A dedicated node group: `instance_type`, `nodes`, `disk_gb`, and `workload` (the node label and taint). Adds the flags `--instance-type`, `--nodes`, `--no-dedicated-nodes`. |
| `core.applications` | Optional. Applications registered in DIAL Core, in Core's configuration format. Tokens: `PLACEHOLDER_SERVICE_URL`, `PLACEHOLDER_THEMES_HOST` and the ones below. |
| `service` | Optional. The component name of the add-on's service (`<platform release>-<service>`). Default: the component of its first application type. |
| `options` | Optional. Settings the customer can change with a flag. `model` is supported: `default` is the DIAL model used unless `--model` is given. The value fills `PLACEHOLDER_MODEL` in `values.yaml` and is remembered in the release. |
| `images[]` | `repository` (the same name is used in the customer's ECR) and optionally `tag` (default: `version`). |
| `chart` | Chart file, relative to `addons/`. |
| `values` | Values file, relative to the add-on folder. |
| `application_types[]` | Application types registered in DIAL Core: `id`, `display_name`, `component`, `deployment`, `configuration_endpoint`, `append_properties_header`, `assistant_attachments`. The service must be named `<platform release>-<component>`, because the schema endpoints are `http://<release>-<component>.<namespace>.svc.cluster.local/...`. |

`values.yaml` is the chart's values with `PLACEHOLDER_<NAME>` tokens where they depend on the installation. The tokens the installer
fills: `DIAL_RELEASE_NAME`, `DIAL_NAMESPACE`, `DIAL_CORE_URL`, `IMAGE_REGISTRY` (the customer's ECR, or the source registry with
`registry.provider: public`), `ADDON_VERSION`, `AWS_REGION`, one token per option (`MODEL`), and `DIAL_API_KEY` (the installation's internal API key from your secret). An unknown token fails the tests. Needs that these tokens cannot express
(a host name, any other secret, a database, an IAM role) are not supported by the catalog yet: they change the infrastructure and need
installer code first.

To publish a new version: push the image with the next `YYYY.M.N` tag. Nothing in this package changes; customers run `add-ons upgrade` and get it.
To add an add-on: copy a folder, change it, and add a row in the tests only if it needs behavior the others do not.

### How registration survives a platform upgrade

DIAL Core's configuration is generated by `apply` and `upgrade` of the platform. So that those never drop an add-on, the add-on's
Helm release carries a marker in its own values, `addon: {id, version, applicationTypeSchemas}`. `deploy.prepare` reads the markers of
the installed add-ons and `values/placeholders.py` appends their schemas to the base ones. There is no state file; the Helm release
is the state. An add-on cannot replace one of the base application types.
