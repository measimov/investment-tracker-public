/** Element Plus rejects failed validation; invalid inputs are an expected user action. */
export async function validateForm(
  form: { validate: () => Promise<unknown> } | null | undefined
): Promise<boolean> {
  if (!form) return false
  try {
    return (await form.validate()) === true
  } catch {
    return false
  }
}
