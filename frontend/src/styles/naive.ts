import type { GlobalThemeOverrides } from 'naive-ui'
import { computed } from 'vue'
import { CHART_FONT_FAMILY } from './tokens'
import { useTheme } from './theme'

/** Both component libraries read the same approved semantic palette. */
export function useHoldingsTheme() {
  const theme = useTheme()
  return computed<GlobalThemeOverrides>(() => {
    void theme.resolved.value
    const styles = getComputedStyle(document.documentElement)
    const color = (name: string) => styles.getPropertyValue(`--app-${name}`).trim()
    return {
      common: {
        baseColor: color('surface'),
        primaryColor: color('primary'),
        primaryColorHover: color('primary-strong'),
        primaryColorPressed: color('primary-pressed'),
        primaryColorSuppl: color('primary'),
        successColor: color('success'),
        successColorHover: color('success-strong'),
        successColorPressed: color('success-pressed'),
        successColorSuppl: color('success'),
        errorColor: color('danger'),
        errorColorHover: color('danger-strong'),
        errorColorPressed: color('danger-pressed'),
        errorColorSuppl: color('danger'),
        warningColor: color('warning'),
        warningColorHover: color('warning-strong'),
        warningColorPressed: color('warning-pressed'),
        warningColorSuppl: color('warning'),
        infoColor: color('info'),
        infoColorHover: color('info-strong'),
        infoColorPressed: color('info-pressed'),
        infoColorSuppl: color('info'),
        textColorBase: color('text'),
        textColor1: color('text'),
        textColor2: color('text'),
        textColor3: color('text-soft'),
        textColorDisabled: color('text-disabled'),
        placeholderColor: color('text-soft'),
        placeholderColorDisabled: color('text-disabled'),
        iconColor: color('text-soft'),
        iconColorHover: color('text-muted'),
        iconColorPressed: color('text'),
        iconColorDisabled: color('text-disabled'),
        dividerColor: color('separator'),
        hoverColor: color('hover'),
        pressedColor: color('primary-soft'),
        inputColorDisabled: color('disabled'),
        tagColor: color('surface-muted'),
        tableHeaderColor: color('surface-muted'),
        tableColorHover: color('hover'),
        tableColorStriped: color('surface-muted'),
        railColor: color('border'),
        progressRailColor: color('surface-muted'),
        scrollbarColor: color('scrollbar'),
        scrollbarColorHover: color('scrollbar-hover'),
        bodyColor: color('bg'),
        cardColor: color('surface'),
        modalColor: color('surface'),
        popoverColor: color('surface'),
        tableColor: color('surface'),
        inputColor: color('surface'),
        actionColor: color('surface-muted'),
        borderColor: color('border'),
        boxShadow1: color('shadow-popover'),
        boxShadow2: color('shadow-popover'),
        boxShadow3: color('shadow-popover'),
        borderRadius: '4px',
        fontFamily: CHART_FONT_FAMILY,
        fontSize: '14px',
        heightMedium: '36px'
      },
      DataTable: {
        thColor: color('surface-muted'),
        thTextColor: color('text-muted'),
        tdColor: color('surface'),
        tdColorHover: color('bg'),
        tdColorSorting: color('surface-muted'),
        borderColor: color('separator'),
        thFontWeight: '400',
        tdPaddingMedium: '16px 14px',
        thPaddingMedium: '12px 14px'
      },
      Button: {
        fontWeight: '500',
        borderRadiusSmall: '6px',
        borderRadiusMedium: '6px',
        borderRadiusLarge: '6px',
        colorPrimary: color('primary-fill'),
        colorHoverPrimary: color('primary-fill-hover'),
        colorPressedPrimary: color('primary-fill-pressed'),
        colorFocusPrimary: color('primary-fill-hover'),
        textColorPrimary: color('on-primary-fill'),
        textColorHoverPrimary: color('on-primary-fill'),
        textColorPressedPrimary: color('on-primary-fill'),
        textColorFocusPrimary: color('on-primary-fill'),
        borderPrimary: `1px solid ${color('primary-fill')}`,
        borderHoverPrimary: `1px solid ${color('primary-fill-hover')}`,
        borderPressedPrimary: `1px solid ${color('primary-fill-pressed')}`,
        borderFocusPrimary: `1px solid ${color('primary-fill-hover')}`,
        textColorInfo: color('on-primary'),
        textColorHoverInfo: color('on-primary'),
        textColorPressedInfo: color('on-primary'),
        ...(theme.resolved.value === 'dark'
          ? {
              textColorSuccess: color('on-primary'),
              textColorHoverSuccess: color('on-primary'),
              textColorPressedSuccess: color('on-primary'),
              textColorError: color('on-primary'),
              textColorHoverError: color('on-primary'),
              textColorPressedError: color('on-primary'),
              textColorWarning: color('on-primary'),
              textColorHoverWarning: color('on-primary'),
              textColorPressedWarning: color('on-primary')
            }
          : {})
      },
      Input: {
        colorDisabled: color('disabled'),
        boxShadowFocus: `0 0 0 2px ${color('focus')}`
      },
      Select: {
        peers: {
          InternalSelection: {
            colorDisabled: color('disabled'),
            boxShadowFocus: `0 0 0 2px ${color('focus')}`
          },
          InternalSelectMenu: {
            optionTextColorActive: color('primary-strong'),
            optionColorPending: color('hover'),
            optionColorActive: color('primary-soft'),
            optionColorActivePending: color('primary-soft')
          }
        }
      },
      Tooltip: {
        color: color('tooltip'),
        textColor: color('tooltip-text')
      },
      Dropdown: {
        optionTextColorActive: color('primary-strong'),
        optionColorActive: color('primary-soft'),
        optionColorHover: color('hover')
      },
      Tag: {
        borderRadius: '3px',
        textColor: color('text-muted'),
        textColorWarning: color('warning-text'),
        textColorInfo: color('info'),
        colorInfo: color('info-soft'),
        colorBorderedInfo: color('info-soft'),
        color: color('surface-muted'),
        colorBordered: color('surface-muted'),
        border: `1px solid ${color('border')}`
      }
    }
  })
}
