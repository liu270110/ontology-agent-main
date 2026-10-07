import Form from '@rjsf/core'
import type {
  BaseInputTemplateProps,
  FieldTemplateProps,
  ObjectFieldTemplateProps,
  RJSFSchema,
  UiSchema,
} from '@rjsf/utils'
import validator from '@rjsf/validator-ajv8'

/** JsonSchemaForm（26 篇 IX-AGT-01 第②步 JsonSchemaForm 首战）：
 *  RJSF 首战结论——@rjsf/shadcn 主题依赖 radix-ui 全家桶 + `@/components/ui/*` 注册表布局，
 *  与本仓库基元体系（modal/sheet/popover + design-system 令牌类）不兼容，覆写超 1 天工作量，
 *  触发任务书既定降级预案（29 篇）：保留 @rjsf/core 引擎（ajv8 Schema 校验 + 状态管理），
 *  自研 FieldTemplate / BaseInputTemplate / ObjectFieldTemplate 约 90 行接 design-system
 *  .field/.input/.field-err 令牌类。颜色全部走令牌（03 篇铁律）。 */

function PlatformFieldTemplate(p: FieldTemplateProps) {
  const { id, label, required, children, rawErrors, rawDescription, displayLabel } = p
  return (
    <div className="field mb-3">
      {displayLabel && (
        <label className="field-label" htmlFor={id}>
          {label}
          {required && (
            <span aria-hidden className="ml-0.5 text-red">
              *
            </span>
          )}
        </label>
      )}
      {children}
      {rawDescription && <div className="fhint">{rawDescription}</div>}
      {rawErrors && <div className="field-err">{rawErrors.join('；')}</div>}
    </div>
  )
}

function PlatformBaseInputTemplate(p: BaseInputTemplateProps) {
  const { id, value, required, disabled, readonly, onChange, options, schema } = p
  const schemaType = Array.isArray(schema.type) ? schema.type[0] : schema.type
  const inputType = (options.inputType as string | undefined) ?? (schemaType === 'integer' || schemaType === 'number' ? 'number' : 'text')
  return (
    <input
      id={id}
      className={`input ${p.rawErrors?.length ? 'err' : ''}`}
      type={inputType}
      value={value ?? ''}
      required={required}
      disabled={disabled}
      readOnly={readonly}
      onChange={e => {
        const raw = e.target.value
        if (inputType === 'number') onChange(raw === '' ? undefined : Number(raw))
        else onChange(raw === '' ? options.emptyValue : raw)
      }}
    />
  )
}

function PlatformObjectTemplate(p: ObjectFieldTemplateProps) {
  return (
    <fieldset className="m-0 border-0 p-0">
      {p.title && !p.properties.length && <legend className="field-label">{p.title}</legend>}
      {p.properties.map(prop => (
        <div key={prop.name}>{prop.content}</div>
      ))}
    </fieldset>
  )
}

const TEMPLATES = {
  FieldTemplate: PlatformFieldTemplate,
  BaseInputTemplate: PlatformBaseInputTemplate,
  ObjectFieldTemplate: PlatformObjectTemplate,
}

export function JsonSchemaForm({
  schema,
  formData,
  onChange,
  disabled,
}: {
  schema: RJSFSchema
  formData: Record<string, unknown>
  onChange: (data: Record<string, unknown>) => void
  disabled?: boolean
}) {
  const uiSchema: UiSchema = {
    'ui:submitButtonOptions': { norender: true },
  }
  return (
    <div data-testid="rjsf-form">
      <Form
        schema={schema}
        uiSchema={uiSchema}
        formData={formData}
        validator={validator}
        templates={TEMPLATES}
        disabled={disabled}
        noHtml5Validate
        onChange={({ formData: fd }) => onChange((fd ?? {}) as Record<string, unknown>)}
      />
    </div>
  )
}
